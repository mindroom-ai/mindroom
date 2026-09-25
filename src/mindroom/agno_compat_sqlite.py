"""Private Agno SQLite bindings and run-persistence repairs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from agno.db.utils import build_single_run_row
from sqlalchemy import event, func, select
from sqlalchemy.dialects.sqlite import insert

from mindroom.usage_storage import project_usage, usage_table_sql, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.db.sqlite import SqliteDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from sqlalchemy import Engine, Row, Table
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


# AGNO_COMPAT: Run-table access for owner transactions uses a private lookup.
# Reason: SqliteDb exposes its runs table only through the private ``_get_table``, while the owner
# must insert rows in its own transaction (usage snapshots, compaction-archive restores).
# Upstream issue: No matching public table-access issue identified; shares the transaction gap below.
# Upstream PR: None identified.
# Remove when: Agno offers a public run-table or caller-owned transaction API; keep owner row semantics.
# Coverage: tests/test_usage_storage.py and tests/test_history_archive.py::test_roll_back_restores_earlier_runs_of_the_hit_generation_in_order.
def run_table(db: SqliteDb) -> Table:
    """Return the runs table, creating it on first use."""
    runs = db._get_table(table_type="runs", create_table_if_not_found=True)
    if runs is None:
        msg = "Run table unavailable"
        raise RuntimeError(msg)
    return runs


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
# Coverage: tests/test_usage_storage.py::test_usage_write_failure_rolls_back_the_run and
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
        ).returning(runs.c.session_id, runs.c.run_id, runs.c.run_data, runs.c.created_at),
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
    runs = run_table(db)
    with db.Session() as transaction, transaction.begin():
        stored = insert_run_row(transaction, runs, run, session_id=session_id, user_id=user_id)
        if record_usage:
            payload = project_usage({**stored.run_data, "created_at": stored.created_at})
            connection = transaction.connection()
            connection.exec_driver_sql(usage_table_sql(db.session_table_name))
            connection.exec_driver_sql(
                usage_upsert_sql(db.session_table_name),
                (stored.session_id, stored.run_id, json.dumps(payload)),
            )


# AGNO_COMPAT: Run deletion and legacy-blob cleanup are not atomic.
# Reason: Agno deletes run rows and scrubs legacy blobs in separate transactions,
# swallows scrub failures, and skips legacy-only databases without a runs table.
# Upstream issue: https://github.com/agno-agi/agno/issues/9934
# Upstream PR: https://github.com/agno-agi/agno/pull/9939
# Remove when: The public adapter atomically deletes rows and legacy blobs, including
# legacy-only databases; retain owner descendant discovery and legacy-format policy.
# Coverage: tests/test_agent_storage_runs.py.
@contextmanager
def run_deletion_transaction(db: SqliteDb) -> Iterator[tuple[Session, Table | None, Table | None]]:
    """Keep the owner's run deletion and legacy scrub inside one Agno transaction."""
    runs_table = db._get_table(table_type="runs")
    sessions_table = db._get_table(table_type="sessions")
    with db.Session() as session, session.begin():
        yield session, runs_table, sessions_table


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
