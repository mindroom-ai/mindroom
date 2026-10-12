"""Private Agno SQLite bindings and run-persistence repairs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.db.utils import build_single_run_row, deserialize_session_json_fields
from sqlalchemy import event, func, select
from sqlalchemy.dialects.sqlite import insert

from mindroom.history.summary_rows_schema import (
    NON_VISIBLE_RUN_STATUSES,
    run_scope_key_sql,
    summary_rows_schema_sql,
    summary_rows_table_names,
)
from mindroom.usage.storage import project_usage, quote_identifier, usage_table_sql, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.db.sqlite import SqliteDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from sqlalchemy import Connection, Engine, Row, Table
    from sqlalchemy.orm import Session

_CONNECT_LISTENER_NAME = "_set_sqlite_pragmas"


# AGNO_COMPAT: SQLite journaling lacks a public configuration hook.
# Reason: SqliteDb unconditionally installs a WAL connect listener without a public
# journal-mode option. The owner must replace it to retain its chosen pragma policy.
# Upstream issue: No matching configurable SQLite pragma issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: SqliteDb exposes configurable pragmas; preserve owner journaling and
# foreign-key behavior without inspecting SQLAlchemy's listener registry.
# Coverage: tests/test_agent_storage_runs.py::test_legacy_runs_blob_is_merged_into_reads_and_deletions_stick.
def remove_default_pragmas(engine: Engine) -> None:
    """Remove exactly the Agno pragma listener before the owner installs its own."""
    connect_listeners = cast("Any", engine.pool.dispatch).connect.listeners
    listeners = [listener for listener in connect_listeners if listener.__name__ == _CONNECT_LISTENER_NAME]
    if len(listeners) != 1:
        msg = f"Expected exactly one Agno {_CONNECT_LISTENER_NAME} connect listener, found {len(listeners)}"
        raise RuntimeError(msg)
    event.remove(engine, "connect", listeners[0])


# AGNO_COMPAT: Run- and session-table access for owner transactions uses a private lookup.
# Reason: SqliteDb exposes its runs and sessions tables only through the private ``_get_table``, while
# the owner must read and write rows in its own transaction (usage snapshots, run deletion, session reads).
# Upstream issue: No matching public table-access issue identified; shares the transaction gap below.
# Upstream PR: None identified.
# Remove when: Agno offers public table access or a caller-owned transaction API; keep owner row semantics.
# Coverage: tests/test_usage_storage.py, tests/test_agent_storage_runs.py::test_deleting_a_team_run_takes_its_member_rows_along,
# and tests/test_helper_usage.py::test_scope_reset_and_first_turn_write_preserve_concurrent_session.
def owned_table(db: SqliteDb, table_type: Literal["runs", "sessions"]) -> Table:
    """Return the runs or sessions table, creating it on first use."""
    table = db._get_table(table_type=table_type, create_table_if_not_found=True)
    if table is None:
        msg = f"SQLite {table_type} table unavailable"
        raise RuntimeError(msg)
    return table


def _existing_table(db: SqliteDb, table_type: Literal["runs", "sessions"]) -> Table | None:
    """Return the runs or sessions table when it exists, without creating it."""
    return db._get_table(table_type=table_type)


# AGNO_COMPAT: Run insertion can reorder surviving stored runs.
# Reason: Agno accepts an in-memory run position that can precede surviving stored
# indexes after deletion. Its MAX+1 path is only used when the supplied index is None.
# Upstream issue: https://github.com/agno-agi/agno/issues/9936
# Upstream PR: https://github.com/agno-agi/agno/pull/9938
# Remove when: The pinned adapter appends new runs after existing indexes even when
# given a shortened session-list position; keep owner prompt sanitization.
# Coverage: tests/test_agent_storage_runs.py::test_runs_appended_after_deleting_leading_runs_sort_after_the_survivors.
# AGNO_COMPAT: Run persistence has no caller-owned transaction extension point.
# Reason: SqliteDb.upsert_run commits internally, preventing atomic application-owned usage writes.
# Upstream issue: No matching transaction extension point identified; the run UPSERT is copied from Agno 3.0.9.
# Upstream PR: None identified for sharing the run transaction.
# Remove when: Agno accepts a caller-owned transaction or an in-transaction persistence hook;
# retain the owner's independent usage retention and atomic snapshot update.
# Coverage: tests/test_usage_storage.py::test_usage_write_failure_rolls_back_the_run,
# tests/test_budget_spend.py::test_spend_charges_a_resumed_run_for_its_requests_this_month, and
# tests/test_history_archive.py::test_roll_back_restores_earlier_runs_of_the_hit_generation_in_order.
def insert_run_row(
    transaction: Session,
    runs: Table,
    run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
) -> Row[Any]:
    """Upsert one run row at the end of its session inside the caller's transaction."""
    row = build_single_run_row(run, session_id=session_id, user_id=user_id, run_index=None)
    row["run_index"] = (
        select(func.coalesce(func.max(runs.c.run_index) + 1, 0))
        .where(runs.c.session_id == session_id)
        .scalar_subquery()
    )
    statement = insert(runs).values(**row)
    return transaction.execute(
        statement.on_conflict_do_update(
            index_elements=["run_id"],
            set_={
                **{
                    name: statement.excluded[name]
                    for name in ("status", "run_data", "user_id", "parent_run_id", "updated_at")
                },
                "run_index": func.coalesce(runs.c.run_index, statement.excluded.run_index),
            },
        ).returning(runs.c.session_id, runs.c.run_id, runs.c.run_data, runs.c.created_at, runs.c.updated_at),
    ).one()


def upsert_run_at_end(
    db: SqliteDb,
    run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
    record_usage: bool = True,
) -> None:
    """Save the run and optional usage in one transaction, preserving Agno's row/index semantics."""
    runs = owned_table(db, "runs")
    with db.Session() as transaction, transaction.begin():
        for statement in summary_rows_schema_sql(db.session_table_name):
            transaction.connection().exec_driver_sql(statement)
        stored = insert_run_row(transaction, runs, run, session_id=session_id, user_id=user_id)
        if record_usage:
            payload = project_usage(
                {**stored.run_data, "created_at": stored.created_at, "updated_at": stored.updated_at},
            )
            connection = transaction.connection()
            connection.exec_driver_sql(usage_table_sql(db.session_table_name))
            connection.exec_driver_sql(
                usage_upsert_sql(db.session_table_name),
                (stored.session_id, stored.run_id, json.dumps(payload)),
            )


@contextmanager
def run_deletion_transaction(db: SqliteDb) -> Iterator[tuple[Session, Table | None]]:
    """Run the owner's run deletion inside one transaction on the private runs table."""
    runs_table = _existing_table(db, "runs")
    with db.Session() as session, session.begin():
        yield session, runs_table


@dataclass(frozen=True)
class _ConversationRead:
    """One session row with the runs a load returns, from one snapshot."""

    session_row: dict[str, Any]
    run_rows: list[tuple[str, str]]


# AGNO_COMPAT: Session reads load every stored run, without one snapshot.
# Reason: SqliteDb.get_session attaches every run row of the session (``runs_limit`` only takes the
# last N), so it cannot stop at the latest summary row, and pysqlite opens no transaction for reads,
# so its session-row and run reads can straddle a concurrent commit.
# Upstream issue: Tracking gap; no matching issue for boundary-filtered or snapshot session reads.
# Upstream PR: None identified.
# Remove when: Agno reads a session's row and a caller-filtered run set in one snapshot; keep the
# owner's filter (runs after the scope's latest summary row, non-visible runs, and their descendants).
# Coverage: tests/test_summary_rows.py::test_load_returns_runs_after_the_latest_summary_and_every_non_visible_run,
# tests/test_summary_rows.py::test_load_keeps_member_rows_with_their_team_run, and
# tests/test_summary_rows.py::test_load_reads_one_snapshot.
def read_conversation(db: SqliteDb, session_id: str) -> _ConversationRead | None:
    """Read one session row and the runs a load returns together."""
    sessions = _existing_table(db, "sessions")
    if sessions is None:
        return None
    runs = _existing_table(db, "runs")
    summaries = summary_rows_table_names(db.session_table_name)[0]
    with db.db_engine.connect() as connection:
        # pysqlite begins a transaction only before a write; an explicit one makes the reads one snapshot.
        connection.exec_driver_sql("BEGIN")
        try:
            row = connection.execute(select(sessions).where(sessions.c.session_id == session_id)).first()
            if row is None:
                return None
            has_summaries = (
                connection.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (summaries,),
                ).first()
                is not None
            )
            run_rows = (
                []
                if runs is None
                else _loaded_run_rows(
                    connection,
                    runs,
                    quote_identifier(summaries) if has_summaries else None,
                    session_id,
                )
            )
        finally:
            connection.rollback()
    return _ConversationRead(session_row=deserialize_session_json_fields(dict(row._mapping)), run_rows=run_rows)


def _loaded_run_rows(
    connection: Connection,
    runs: Table,
    summaries: str | None,
    session_id: str,
) -> list[tuple[str, str]]:
    """Return ``(run_id, run_data text)`` of the runs after their scope's latest summary row, non-visible runs, and descendants."""
    runs_name = quote_identifier(runs.name)
    hidden = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUSES)
    # A database that has not written a run since it gained a sessions table has no summary rows yet.
    # A scope without summary rows, and a row without an index, compare as NULL and load.
    after_latest = (
        f"AND (run.status IN ({hidden}) OR COALESCE(run.run_index > (SELECT row.after_run_index FROM {summaries} AS row "  # noqa: S608
        f"WHERE row.session_id = run.session_id AND row.scope_key = {run_scope_key_sql('run')} "
        "ORDER BY row.id DESC LIMIT 1), 1)) "
        if summaries is not None
        else ""
    )
    rows = connection.exec_driver_sql(
        f"WITH RECURSIVE loaded(run_id) AS (SELECT run.run_id FROM {runs_name} AS run "  # noqa: S608
        f"WHERE run.session_id = ? AND run.parent_run_id IS NULL {after_latest}"
        f"UNION SELECT child.run_id FROM {runs_name} AS child JOIN loaded ON child.parent_run_id = loaded.run_id "
        "WHERE child.session_id = ?) "
        f"SELECT run.run_id, CAST(run.run_data AS TEXT) FROM {runs_name} AS run "
        "WHERE run.session_id = ? AND run.run_id IN (SELECT run_id FROM loaded) "
        "ORDER BY run.run_index, run.created_at, run.run_id",
        (session_id, session_id, session_id),
    )
    return [(run_id, run_data) for run_id, run_data in rows]


# AGNO_COMPAT: Session-cache diagnostics require private counters.
# Reason: Agno exposes no cache statistics; diagnostics currently read its private
# per-session run map. Sampling, aggregation and storage lifetime belong to the owner.
# Upstream issue: No matching public session-cache statistics issue identified.
# Upstream PR: None identified for this observability API.
# Remove when: Public cache counters expose the same session/run counts.
# Coverage: tests/test_agent_storage_runs.py.
def cached_run_counts(db: SqliteDb) -> tuple[int, int]:
    """Count this adapter's cached sessions/runs without retaining their contents."""
    sessions = db._run_object_cache._per_session
    return len(sessions), sum(len(runs) for runs in sessions.values())
