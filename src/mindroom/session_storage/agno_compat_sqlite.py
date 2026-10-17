"""Private Agno SQLite bindings and run-persistence repairs."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.db.base import SessionType
from agno.db.utils import build_single_run_row, deserialize_session, deserialize_session_json_fields
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from sqlalchemy import and_, case, event, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError

from mindroom.history.summary_rows_schema import (
    NON_VISIBLE_RUN_STATUS_VALUES,
    latest_row_index_sql,
    loads_sql,
    owner_scope_key,
    summary_rows_schema_sql,
    summary_rows_table_names,
)
from mindroom.usage.storage import project_usage, quote_identifier, usage_table_sql, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from agno.db.sqlite import SqliteDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from agno.session import Session as AgnoSession
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
# Coverage: tests/test_agent_storage_runs.py::test_agno_2_blob_runs_become_rows_once.
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
    # AGNO_COMPAT: Table creation is check-then-create across storage objects.
    # Reason: SqliteDb serializes table resolution only within one storage object, then checks for the table
    # and issues a plain CREATE TABLE, so two owners opening one fresh conversation database at once can both
    # find it missing and the later CREATE fails with "already exists", failing that owner's reply.
    # Upstream issue: Tracking gap: no issue identified for concurrent table creation across SqliteDb objects.
    # Upstream PR: None identified.
    # Remove when: SqliteDb creates tables with IF NOT EXISTS or tolerates a concurrent create.
    # Coverage: tests/test_agent_storage_runs.py::test_storage_opens_when_another_owner_creates_the_runs_table_first.
    try:
        table = db._get_table(table_type=table_type, create_table_if_not_found=True)
    except OperationalError as error:
        if "already exists" not in str(error.orig):
            raise
        # Another owner created it between this owner's check and its CREATE; resolve the existing table.
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
# given a shortened session-list position, and lets the owner allocate the index and veto an
# insert or update inside its UPSERT; keep owner prompt sanitization and the conversation-log rules.
# Coverage: tests/test_agent_storage_runs.py::test_runs_appended_after_deleting_leading_runs_sort_after_the_survivors
# and tests/test_agent_storage_runs.py::test_new_runs_sort_after_every_summary_row.
# AGNO_COMPAT: Run persistence has no caller-owned transaction extension point.
# Reason: SqliteDb.upsert_run commits internally, preventing atomic application-owned usage writes.
# Upstream issue: No matching transaction extension point identified; the run UPSERT is copied from Agno 3.0.9.
# Upstream PR: None identified for sharing the run transaction.
# Remove when: Agno accepts a caller-owned transaction or an in-transaction persistence hook;
# retain the owner's independent usage retention and atomic snapshot update.
# Coverage: tests/test_usage_storage.py::test_usage_write_failure_rolls_back_the_run,
# tests/test_usage_storage.py::test_a_refused_save_of_a_removed_run_still_records_usage, and
# tests/test_budget_spend.py::test_spend_charges_a_resumed_run_for_its_requests_this_month.
def _insert_run_row(
    transaction: Session,
    runs: Table,
    run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
    session_table: str,
) -> Row[Any] | Literal["removed", "frozen"]:
    """Upsert one run row inside the caller's write transaction, under the conversation log's rules.

    A run a cut removed, or a member of one, writes no row (``"removed"``). A visible run behind
    its scope's latest summary row, or a member of one, is folded and stays as stored
    (``"frozen"``). A run new to the session, or saved visible over a stored non-visible row
    such as a completed approval continuation, takes the next index after every run and summary row.
    """
    row = build_single_run_row(run, session_id=session_id, user_id=user_id, run_index=None)
    connection = transaction.connection()
    summaries, removed = (quote_identifier(name) for name in summary_rows_table_names(session_table))
    runs_name = quote_identifier(runs.name)
    if (
        connection.exec_driver_sql(
            f"SELECT 1 FROM {removed} WHERE run_id IN (?, ?)",  # noqa: S608
            (row["run_id"], row["parent_run_id"]),
        ).first()
        is not None
    ):
        return "removed"
    if _is_folded(connection, runs_name, summaries, row["run_id"], row["parent_run_id"]):
        return "frozen"
    row["run_index"] = connection.exec_driver_sql(
        f"SELECT MAX(COALESCE((SELECT MAX(run_index) FROM {runs_name} WHERE session_id = ?), -1), "  # noqa: S608
        f"COALESCE((SELECT MAX(after_run_index) FROM {summaries} WHERE session_id = ?), -1)) + 1",
        (session_id, session_id),
    ).scalar_one()
    statement = insert(runs).values(**row)
    becomes_visible = and_(
        runs.c.parent_run_id.is_(None),
        runs.c.status.in_(NON_VISIBLE_RUN_STATUS_VALUES),
        statement.excluded.parent_run_id.is_(None),
        statement.excluded.status.not_in(NON_VISIBLE_RUN_STATUS_VALUES),
    )
    return transaction.execute(
        statement.on_conflict_do_update(
            index_elements=["run_id"],
            set_={
                **{
                    name: statement.excluded[name]
                    for name in ("status", "run_data", "user_id", "parent_run_id", "updated_at")
                },
                # The model produced the rest of such a run after the turns it waited for, so it sorts after them.
                "run_index": case(
                    (becomes_visible, statement.excluded.run_index),
                    else_=func.coalesce(runs.c.run_index, statement.excluded.run_index),
                ),
            },
        ).returning(runs.c.session_id, runs.c.run_id, runs.c.run_data, runs.c.created_at, runs.c.updated_at),
    ).one()


def _is_folded(connection: Connection, runs: str, summaries: str, run_id: str, parent_run_id: str | None) -> bool:
    """Return whether a run, or the top-level run it belongs to, is visible and behind its scope's latest summary row.

    The chain starts at the stored run and at the parent the save names, so a member row seen for the
    first time is as final as the folded team run it belongs to.
    """
    return (
        connection.exec_driver_sql(
            "WITH RECURSIVE chain(run_id, parent_run_id, run_index, status, run_type, agent_id, team_id, session_id) "  # noqa: S608
            f"AS (SELECT run_id, parent_run_id, run_index, status, run_type, agent_id, team_id, session_id FROM {runs} "
            "WHERE run_id IN (?, ?) UNION SELECT run.run_id, run.parent_run_id, run.run_index, run.status, run.run_type, "
            f"run.agent_id, run.team_id, run.session_id FROM {runs} AS run JOIN chain ON run.run_id = chain.parent_run_id) "
            f"SELECT 1 FROM chain AS top WHERE top.parent_run_id IS NULL "
            f"AND NOT {loads_sql('top', latest_row_index_sql(summaries, 'top'))}",
            (run_id, parent_run_id),
        ).first()
        is not None
    )


def upsert_run_at_end(
    db: SqliteDb,
    run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
    record_usage: bool = True,
) -> Literal["written", "removed", "frozen"]:
    """Save the run and optional usage in one transaction, under the conversation log's insertion rules.

    Returns whether the run row was written, refused because a cut removed the run, or refused
    because the run is folded behind its scope's latest summary row.
    """
    runs = owned_table(db, "runs")
    with db.Session() as transaction, transaction.begin():
        connection = transaction.connection()
        # The insertion rules read the log before they write, so the write lock comes first.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        for statement in summary_rows_schema_sql(db.session_table_name):
            connection.exec_driver_sql(statement)
        stored = _insert_run_row(
            transaction,
            runs,
            run,
            session_id=session_id,
            user_id=user_id,
            session_table=db.session_table_name,
        )
        outcome: Literal["written", "removed", "frozen"] = stored if isinstance(stored, str) else "written"
        if not record_usage or stored == "frozen":
            # A folded run's usage is final; a stale copy must not replace it.
            return outcome
        if stored == "removed":
            # The provider calls behind a save of a cut run were made; the ledger outlives deleted runs.
            run_data = cast("dict[str, Any]", run) if isinstance(run, dict) else run.to_dict()
            now = int(time.time())
            payload_run = {**run_data, "created_at": run_data.get("created_at") or now, "updated_at": now}
            run_id = run_data["run_id"]
        else:
            payload_run = {**stored.run_data, "created_at": stored.created_at, "updated_at": stored.updated_at}
            run_id = stored.run_id
        connection.exec_driver_sql(usage_table_sql(db.session_table_name))
        connection.exec_driver_sql(
            usage_upsert_sql(db.session_table_name),
            (session_id, run_id, json.dumps(project_usage(payload_run))),
        )
    return outcome


@contextmanager
def run_deletion_transaction(db: SqliteDb) -> Iterator[tuple[Session, Table | None]]:
    """Run the owner's run deletion inside one transaction on the private runs table."""
    runs_table = _existing_table(db, "runs")
    with db.Session() as session, session.begin():
        yield session, runs_table


_ID_CHUNK_SIZE = 500


def delete_run_subtrees(
    connection: Connection,
    runs: str,
    run_ids: Collection[str],
    *,
    session_id: str | None,
) -> set[str]:
    """Delete ``run_ids`` with every member run below them, within one session when given; return the deleted ids.

    ``runs`` is the quoted runs table; the caller owns the transaction. Team member runs are rows
    whose ``parent_run_id`` is the team run, so a deleted run takes its whole subtree along.
    """
    scope, scope_parameters = ("session_id = ? AND ", (session_id,)) if session_id is not None else ("", ())
    found = {run_id for run_id in run_ids if run_id}
    frontier = sorted(found)
    while frontier:
        chunk, frontier = frontier[:_ID_CHUNK_SIZE], frontier[_ID_CHUNK_SIZE:]
        children = [
            run_id
            for (run_id,) in connection.exec_driver_sql(
                f"SELECT run_id FROM {runs} WHERE {scope}parent_run_id IN ({', '.join('?' for _ in chunk)})",  # noqa: S608
                (*scope_parameters, *chunk),
            )
            if run_id not in found
        ]
        found.update(children)
        frontier.extend(children)
    ordered = sorted(found)
    for start in range(0, len(ordered), _ID_CHUNK_SIZE):
        chunk = ordered[start : start + _ID_CHUNK_SIZE]
        connection.exec_driver_sql(
            f"DELETE FROM {runs} WHERE {scope}run_id IN ({', '.join('?' for _ in chunk)})",  # noqa: S608
            (*scope_parameters, *chunk),
        )
    return found


# AGNO_COMPAT: Session reads load every stored run, without one snapshot.
# Reason: SqliteDb.get_session attaches every run row of the session (``runs_limit`` only takes the
# last N), so it cannot stop at the latest summary row, and pysqlite opens no transaction for reads,
# so its session-row and run reads can straddle a concurrent commit. Building the session here
# reuses Agno's deserialization and its private run-object cache, as SqliteDb.get_session does.
# Upstream issue: Tracking gap; no matching issue for boundary-filtered or snapshot session reads.
# Upstream PR: None identified.
# Remove when: Agno reads a session's row and a caller-filtered run set in one snapshot; keep the
# owner's filter (runs after the scope's latest summary row, non-visible runs, and their descendants).
# Coverage: tests/test_summary_rows.py::test_load_returns_runs_after_the_latest_summary_and_every_non_visible_run,
# tests/test_summary_rows.py::test_load_keeps_member_rows_with_their_team_run,
# tests/test_summary_rows.py::test_load_reads_one_snapshot, and
# tests/test_summary_rows.py::test_load_finds_member_rows_without_scanning_the_session_per_run.
def read_session(
    db: SqliteDb,
    session_id: str,
    session_type: SessionType | None,
    *,
    deserialize: bool,
) -> AgnoSession | dict[str, Any] | None:
    """Read one session with the runs a load returns and its replayed summary, from one snapshot, as Agno would build it."""
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
            session_raw = deserialize_session_json_fields(dict(row._mapping))
            owner = owner_scope_key(
                session_raw.get("session_type"),
                session_raw.get("agent_id"),
                session_raw.get("team_id"),
            )
            # The summary replays from the owner scope's latest summary row; the session row's copy is ignored.
            latest = (
                connection.exec_driver_sql(
                    f"SELECT summary, created_at FROM {quote_identifier(summaries)} "  # noqa: S608
                    "WHERE session_id = ? AND scope_key = ? ORDER BY id DESC LIMIT 1",
                    (session_id, owner),
                ).first()
                if has_summaries and owner is not None
                else None
            )
        finally:
            connection.rollback()
    summary = (
        SessionSummary(summary=latest[0], updated_at=datetime.fromtimestamp(latest[1], UTC))
        if latest is not None and latest[0] is not None
        else None
    )
    cached_runs = deserialize and session_raw.get("session_type") == SessionType.AGENT.value
    session_raw["runs"] = None if cached_runs else [json.loads(run_data) for _run_id, run_data in run_rows]
    if not deserialize:
        session_raw["summary"] = summary.to_dict() if summary is not None else None
        return session_raw
    session_raw["summary"] = None
    session = deserialize_session(session_type, session_raw)
    if isinstance(session, (AgentSession, TeamSession)):
        session.summary = summary
    if cached_runs:
        session.runs = db._run_object_cache.runs_from_rows(session_id, run_rows)  # type: ignore[union-attr]
    return session


def _loaded_run_rows(
    connection: Connection,
    runs: Table,
    summaries: str | None,
    session_id: str,
) -> list[tuple[str, str]]:
    """Return ``(run_id, run_data text)`` of the runs after their scope's latest summary row, non-visible runs, and descendants."""
    runs_name = quote_identifier(runs.name)
    # A database that has not written a run since it gained a sessions table has no summary rows yet.
    after_latest = f"AND {loads_sql('run', latest_row_index_sql(summaries, 'run'))} " if summaries is not None else ""
    # The runs table has no parent index, so the descendant step searches a materialized child set
    # (SQLite builds an automatic index on it) instead of scanning the session once per loaded run.
    rows = connection.exec_driver_sql(
        "WITH RECURSIVE children(run_id, parent_run_id) AS MATERIALIZED ("  # noqa: S608
        f"SELECT run_id, parent_run_id FROM {runs_name} WHERE session_id = ? AND parent_run_id IS NOT NULL), "
        f"loaded(run_id) AS (SELECT run.run_id FROM {runs_name} AS run "
        f"WHERE run.session_id = ? AND run.parent_run_id IS NULL {after_latest}"
        "UNION SELECT children.run_id FROM loaded JOIN children ON children.parent_run_id = loaded.run_id) "
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
