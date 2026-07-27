"""The thread-cache statements, written once and rendered per backend.

Every statement here was previously transcribed by hand into both backend modules. The pair had
already drifted twice by the time this was written, in ways a reader could only find by diffing
them: the same upsert expressed as ``GREATEST`` on one side and ``MAX(COALESCE(...))`` on the
other, and ``excluded`` spelled in two cases.

The statements bind every parameter by name. The positional form is what allowed one statement to
pass the same value three times running, where the argument order was load-bearing and silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .event_cache_sql_dialect import (
    EVENT_EDITS,
    EVENTS,
    ROOM_STATE,
    THREAD_EVENTS,
    THREAD_STATE,
)

if TYPE_CHECKING:
    from .event_cache_sql_dialect import SqlDialect


def _surviving_edits_cte(d: SqlDialect) -> str:
    """Return the CTE selecting the edits that survive a collapsed read.

    One per (original, sender). The prose behind every clause - why the sender comparison here is
    an optimization rather than the security boundary, why the original is LEFT joined out of the
    events table alone, why ROW_NUMBER rather than a correlated NOT EXISTS, and what a write-time
    prune would have to preserve - is recorded once above ``load_thread_events`` in
    ``event_cache_thread_ops``.

    Do not rewrite the ranking as ``DISTINCT ON (original_event_id)``: that shape materialises
    every row of the thread before it can pick winners.

    Do not re-derive this query's cost without ANALYZE. On unanalyzed tables an unseen scope
    estimates 1 row against thousands actual, every join degrades to a nested loop with a join
    filter, and every shape collapses - a plain unfiltered read of the same thread included.
    """
    scope = d.scope_column
    return f"""
WITH surviving_edits AS MATERIALIZED (
    SELECT edit_event_id
    FROM (
        SELECT event_edits.edit_event_id AS edit_event_id,
               ROW_NUMBER() OVER (
                   PARTITION BY event_edits.original_event_id
                   ORDER BY event_edits.origin_server_ts DESC,
                            event_edits.edit_event_id{d.binary_collation} DESC
               ) AS edit_rank
        FROM {d.table_as(EVENT_EDITS)}
        JOIN {d.table_as(THREAD_EVENTS, "edit_membership")}
            ON edit_membership.{scope} = event_edits.{scope}
            AND edit_membership.room_id = event_edits.room_id
            AND edit_membership.event_id = event_edits.edit_event_id
            AND edit_membership.thread_id = {d.parameter("thread_id")}
        JOIN {d.table_as(EVENTS, "edit_events")}
            ON edit_events.{scope} = event_edits.{scope}
            AND edit_events.room_id = event_edits.room_id
            AND edit_events.event_id = event_edits.edit_event_id
        LEFT JOIN {d.table_as(EVENTS, "original_events")}
            ON original_events.{scope} = event_edits.{scope}
            AND original_events.room_id = event_edits.room_id
            AND original_events.event_id = event_edits.original_event_id
        WHERE event_edits.{scope} = {d.parameter("scope")}
            AND event_edits.room_id = {d.parameter("room_id")}
            AND (original_events.event_id IS NULL OR edit_events.sender = original_events.sender)
    ) AS ranked
    WHERE edit_rank = 1
)
"""  # noqa: S608 - every interpolation is a dialect-rendered literal; params stay bound


@dataclass(frozen=True)
class ThreadStatements:
    """Every thread-cache statement, rendered for one backend."""

    thread_events: str
    thread_event_ids: str
    recent_room_thread_ids: str
    thread_cache_gap: str
    room_membership: str
    certify_room_membership: str
    set_room_membership: str
    upsert_thread_membership: str
    snapshot_fetch_started_at: str
    room_gap: str
    record_snapshot_keeping_uncovered_gap: str
    delete_thread_membership_by_thread: str
    delete_thread_membership_by_room: str
    delete_thread_state_by_thread: str
    delete_thread_state_by_room: str
    mark_thread_gap: str
    fan_out_room_gap: str
    upsert_room_gap: str
    thread_has_readable_event: str
    advance_snapshot_watermark: str
    event_ids_for_thread: str
    event_ids_for_room: str

    @classmethod
    def build(cls, d: SqlDialect) -> ThreadStatements:
        """Render every thread-cache statement for one backend."""
        scope = d.scope_column
        p_scope = d.parameter("scope")
        p_room = d.parameter("room_id")
        p_thread = d.parameter("thread_id")
        scoped_room = f"{scope} = {p_scope} AND room_id = {p_room}"
        thread_events = d.table(THREAD_EVENTS)
        thread_state = d.table(THREAD_STATE)
        room_state = d.table(ROOM_STATE)
        upsert = d.thread_event_upsert

        # One thread, collapsed: every non-edit row, plus the one surviving edit per edited message.
        thread_events_sql = (
            _surviving_edits_cte(d)  # noqa: S608 - dialect-rendered literals only; params stay bound
            + f"""
SELECT thread_events.origin_server_ts, thread_events.write_seq, events.event_json
FROM {d.table_as(THREAD_EVENTS)}
JOIN {d.table_as(EVENTS)}
    ON events.{scope} = thread_events.{scope}
    AND events.room_id = thread_events.room_id
    AND events.event_id = thread_events.event_id
WHERE thread_events.{scope} = {p_scope}
    AND thread_events.room_id = {p_room}
    AND thread_events.thread_id = {p_thread}
    AND (
        NOT EXISTS (
            SELECT 1
            FROM {d.table_as(EVENT_EDITS, "row_is_an_edit")}
            WHERE row_is_an_edit.{scope} = thread_events.{scope}
                AND row_is_an_edit.room_id = thread_events.room_id
                AND row_is_an_edit.edit_event_id = thread_events.event_id
        )
        OR thread_events.event_id IN (SELECT edit_event_id FROM surviving_edits)
    )
ORDER BY thread_events.origin_server_ts ASC, thread_events.write_seq ASC
"""  # noqa: S608 - every interpolation is a dialect-rendered literal; params stay bound
        )

        # Joined to the payload table rather than reading membership alone: a membership row whose
        # payload is gone is not durably present, and reporting it as present suppresses a refill
        # that should happen.
        readable_thread_rows = f"""
        FROM {d.table_as(THREAD_EVENTS)}
        JOIN {d.table_as(EVENTS)}
            ON events.{scope} = thread_events.{scope}
            AND events.room_id = thread_events.room_id
            AND events.event_id = thread_events.event_id
        WHERE thread_events.{scope} = {p_scope}
            AND thread_events.room_id = {p_room}
            AND thread_events.thread_id = {p_thread}
        """

        return cls(
            thread_events=thread_events_sql,
            thread_event_ids=f"SELECT thread_events.event_id{readable_thread_rows}",
            thread_has_readable_event=f"SELECT 1{readable_thread_rows}LIMIT 1",
            recent_room_thread_ids=f"""
        SELECT thread_id
        FROM {thread_events}
        WHERE {scoped_room}
        GROUP BY thread_id
        ORDER BY MAX(origin_server_ts) DESC, thread_id ASC
        LIMIT {d.parameter("limit")}
        """,  # noqa: S608
            thread_cache_gap=f"""
        SELECT gap_marked_at, gap_reason
        FROM {thread_state}
        WHERE {scoped_room} AND thread_id = {p_thread}
        """,  # noqa: S608
            room_membership=f"""
        SELECT membership_state, membership_epoch
        FROM {room_state}
        WHERE {scoped_room}
        """,  # noqa: S608
            # ``DO NOTHING`` rather than SQLite's former ``INSERT OR IGNORE``: the statement
            # supplies every NOT NULL column, so the primary key is the only constraint either
            # form could have been ignoring, and naming it is the narrower of the two.
            certify_room_membership=f"""
        INSERT INTO {room_state}(
            {scope},
            room_id,
            membership_state,
            membership_epoch
        )
        VALUES ({p_scope}, {p_room}, 'joined', 0)
        ON CONFLICT({scope}, room_id) DO NOTHING
        """,  # noqa: S608
            set_room_membership=f"""
        UPDATE {room_state}
        SET membership_state = {d.parameter("membership_state")},
            membership_epoch = membership_epoch + 1
        WHERE {scoped_room}
        """,  # noqa: S608
            upsert_thread_membership=f"""
        INSERT INTO {thread_events}(
            {scope},
            room_id,
            thread_id,
            event_id,
            origin_server_ts{upsert.insert_column}
        )
        VALUES (
            {p_scope},
            {p_room},
            {p_thread},
            {d.parameter("event_id")},
            {d.parameter("origin_server_ts")}{upsert.insert_value}
        )
        ON CONFLICT({scope}, room_id, event_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            origin_server_ts = excluded.origin_server_ts,
{upsert.conflict_assignments}
        """,  # noqa: S608
            snapshot_fetch_started_at=f"""
        SELECT snapshot_fetch_started_at
        FROM {thread_state}
        WHERE {scoped_room} AND thread_id = {p_thread}
        """,  # noqa: S608
            room_gap=f"""
        SELECT room_gap_marked_at, room_gap_reason
        FROM {room_state}
        WHERE {scoped_room}
        """,  # noqa: S608
            # A gap marked after the fetch began describes events the fetch could not have seen, so
            # it survives and the next read refetches.
            record_snapshot_keeping_uncovered_gap=f"""
        INSERT INTO {thread_state}(
            {scope},
            room_id,
            thread_id,
            gap_marked_at,
            gap_reason,
            snapshot_fetch_started_at
        )
        VALUES (
            {p_scope},
            {p_room},
            {p_thread},
            {d.parameter("room_gap_marked_at")},
            {d.parameter("room_gap_reason")},
            {d.parameter("fetch_started_at")}
        )
        ON CONFLICT({scope}, room_id, thread_id) DO UPDATE SET
            snapshot_fetch_started_at = excluded.snapshot_fetch_started_at,
            gap_marked_at = CASE
                WHEN {thread_state}.gap_marked_at IS NULL
                    OR {thread_state}.gap_marked_at <= {d.parameter("fetch_started_at")}
                    THEN excluded.gap_marked_at
                ELSE {thread_state}.gap_marked_at
            END,
            gap_reason = CASE
                WHEN {thread_state}.gap_marked_at IS NULL
                    OR {thread_state}.gap_marked_at <= {d.parameter("fetch_started_at")}
                    THEN excluded.gap_reason
                ELSE {thread_state}.gap_reason
            END
        """,  # noqa: S608
            delete_thread_membership_by_thread=f"""
        DELETE FROM {thread_events}
        WHERE {scoped_room} AND thread_id = {p_thread}
        """,  # noqa: S608
            delete_thread_membership_by_room=f"""
        DELETE FROM {thread_events}
        WHERE {scoped_room}
        """,  # noqa: S608
            delete_thread_state_by_thread=f"""
        DELETE FROM {thread_state}
        WHERE {scoped_room} AND thread_id = {p_thread}
        """,  # noqa: S608
            delete_thread_state_by_room=f"""
        DELETE FROM {thread_state}
        WHERE {scoped_room}
        """,  # noqa: S608
            # Monotonic: a later gap never loses to an earlier one. There is no reason precedence -
            # every reason means the same thing, that this snapshot must be refetched.
            mark_thread_gap=f"""
        INSERT INTO {thread_state}(
            {scope},
            room_id,
            thread_id,
            gap_marked_at,
            gap_reason
        )
        VALUES (
            {p_scope},
            {p_room},
            {p_thread},
            {d.parameter("gap_marked_at")},
            {d.parameter("gap_reason")}
        )
        ON CONFLICT({scope}, room_id, thread_id) DO UPDATE SET
            gap_marked_at = {d.monotonic_max(f"{thread_state}.gap_marked_at", "excluded.gap_marked_at")},
            gap_reason = CASE
                WHEN {thread_state}.gap_marked_at IS NULL
                    OR excluded.gap_marked_at >= {thread_state}.gap_marked_at
                    THEN excluded.gap_reason
                ELSE {thread_state}.gap_reason
            END
        """,  # noqa: S608
            # The room-scoped fan-out. Every SET expression reads the pre-update row, so the two
            # assignments are order-independent.
            fan_out_room_gap=f"""
        UPDATE {thread_state}
        SET gap_reason = CASE
                WHEN gap_marked_at IS NULL
                    OR {d.parameter("gap_marked_at")} >= gap_marked_at
                    THEN {d.parameter("gap_reason")}
                ELSE gap_reason
            END,
            gap_marked_at = {d.monotonic_max("gap_marked_at", d.parameter("gap_marked_at"))}
        WHERE {scoped_room}
        """,  # noqa: S608
            upsert_room_gap=f"""
        INSERT INTO {room_state}(
            {scope},
            room_id,
            room_gap_marked_at,
            room_gap_reason
        )
        VALUES (
            {p_scope},
            {p_room},
            {d.parameter("gap_marked_at")},
            {d.parameter("gap_reason")}
        )
        ON CONFLICT({scope}, room_id) DO UPDATE SET
            room_gap_reason = CASE
                WHEN {room_state}.room_gap_marked_at IS NULL
                    OR excluded.room_gap_marked_at >= {room_state}.room_gap_marked_at
                    THEN excluded.room_gap_reason
                ELSE {room_state}.room_gap_reason
            END,
            room_gap_marked_at = {
                d.monotonic_max(
                    f"{room_state}.room_gap_marked_at",
                    "excluded.room_gap_marked_at",
                )
            }
        """,  # noqa: S608
            advance_snapshot_watermark=f"""
        INSERT INTO {thread_state}(
            {scope},
            room_id,
            thread_id,
            snapshot_fetch_started_at
        )
        VALUES (
            {p_scope},
            {p_room},
            {p_thread},
            {d.parameter("reflected_at")}
        )
        ON CONFLICT({scope}, room_id, thread_id) DO UPDATE SET
            snapshot_fetch_started_at = {
                d.monotonic_max(
                    f"{thread_state}.snapshot_fetch_started_at",
                    "excluded.snapshot_fetch_started_at",
                )
            }
        """,  # noqa: S608
            event_ids_for_thread=f"""
        SELECT event_id
        FROM {thread_events}
        WHERE {scoped_room} AND thread_id = {p_thread}
        """,  # noqa: S608
            event_ids_for_room=f"""
        SELECT event_id
        FROM {thread_events}
        WHERE {scoped_room}
        """,  # noqa: S608
        )
