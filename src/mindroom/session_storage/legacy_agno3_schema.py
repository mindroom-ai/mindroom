"""The Agno layout of a conversation database before storage v2, which only its conversion reads.

The releases before storage v2 kept Agno 3's sessions and runs tables, column for column and index for index, with
MindRoom's summary-row, removed-run, and usage tables beside them, and Agno's own ``agno_schema_versions``.
``AgnoTableNames`` names those tables, ``agno_schema_sql`` creates them, ``run_from_row`` decodes a runs-table row, and
``delete_run_subtrees_in_transaction`` deletes runs with the member runs below them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.session_storage.legacy_tool_results import split_combined_tool_results
from mindroom.session_storage.schema import TableNames
from mindroom.usage.storage import quote_identifier, usage_table_sql

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection

    from mindroom.llm.runs import AnyRunRecord

# Agno's record of the layout version it created each table with, one table per database whatever its storage name.
AGNO_SCHEMA_VERSIONS_TABLE = "agno_schema_versions"
_SESSION_COLUMN_TYPES = {
    "session_id": "VARCHAR NOT NULL",
    "session_type": "VARCHAR NOT NULL",
    "agent_id": "VARCHAR",
    "team_id": "VARCHAR",
    "workflow_id": "VARCHAR",
    "user_id": "VARCHAR",
    "session_data": "JSON",
    "agent_data": "JSON",
    "team_data": "JSON",
    "workflow_data": "JSON",
    "metadata": "JSON",
    "summary": "JSON",
    "created_at": "BIGINT NOT NULL",
    "updated_at": "BIGINT",
}
_RUNS_INDEXED_COLUMNS = (
    "session_id",
    "run_type",
    "agent_id",
    "team_id",
    "workflow_id",
    "user_id",
    "status",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class AgnoTableNames(TableNames):
    """The names of one conversation database, with the tables of its Agno layout beside the current ones."""

    @property
    def runs(self) -> str:
        """The Agno runs table; only the storage name ``agno`` kept Agno's own name for it."""
        return "agno_runs" if self.storage_name == "agno" else f"{self.sessions}_runs"

    @property
    def summaries(self) -> str:
        """The summary-row table, positioned by ``run_index``."""
        return f"{self.sessions}_summaries"

    @property
    def removed_runs(self) -> str:
        """The table of run ids a cut removed."""
        return f"{self.sessions}_removed_runs"


# LEGACY_COMPAT: The Agno 3 layout of a conversation database.
# Legacy format: Agno 3's fourteen-column sessions table and its runs table (`agno_runs` for the storage name `agno`)
# with their indexes, the summary-row and removed-run tables, and the usage ledger; a database an older release wrote
# lacks some of them.
# Last legacy release: the last release before storage v2; replacement: unreleased, `log_schema_sql`.
# Handling: The conversion's Agno-era steps add the tables they introduced, and the conversion then creates any the
# database still lacks, empty, so every step reads its rows from a table that exists; it drops them once copied.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_database_missing_an_agno_table_converts.
def agno_schema_sql(names: AgnoTableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of every table and index of the last Agno-layout release."""
    return (*agno_runs_sql(names), *agno_summary_rows_sql(names), usage_table_sql(names.sessions))


def agno_runs_sql(names: AgnoTableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of the Agno 3 sessions and runs tables alone."""
    sessions, runs = quote_identifier(names.sessions), quote_identifier(names.runs)
    session_columns = ", ".join(f"{column} {column_type}" for column, column_type in _SESSION_COLUMN_TYPES.items())
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} ({session_columns}, PRIMARY KEY (session_id))",
        _index_sql(names.sessions, "session_type"),
        _index_sql(names.sessions, "created_at"),
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, "
        "run_type VARCHAR NOT NULL, agent_id VARCHAR, team_id VARCHAR, workflow_id VARCHAR, user_id VARCHAR, "
        "parent_run_id VARCHAR, status VARCHAR, run_index BIGINT, run_data JSON NOT NULL, "
        "created_at BIGINT NOT NULL, updated_at BIGINT, PRIMARY KEY (run_id), "
        f"FOREIGN KEY(session_id) REFERENCES {sessions} (session_id) ON DELETE CASCADE)",
        *(_index_sql(names.runs, column) for column in _RUNS_INDEXED_COLUMNS),
        _index_sql(names.runs, "session_id", "run_index"),
    )


def agno_summary_rows_sql(names: AgnoTableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of the summary-row and removed-run tables beside the Agno runs table."""
    sessions = quote_identifier(names.sessions)
    summaries, removed_runs = quote_identifier(names.summaries), quote_identifier(names.removed_runs)
    return (
        f"CREATE TABLE IF NOT EXISTS {summaries} (id INTEGER PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, "
        "scope_key TEXT NOT NULL, after_run_index INTEGER NOT NULL, summary TEXT, summary_model TEXT, "
        "event_ids TEXT NOT NULL DEFAULT '[]', seen_event_ids TEXT NOT NULL DEFAULT '[]', "
        "legacy INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, segment_run_id TEXT, segment TEXT)",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(names.sessions + '_summaries_scope')} "
        f"ON {summaries} (session_id, scope_key, id)",
        f"CREATE TABLE IF NOT EXISTS {removed_runs} (run_id TEXT PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE)",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(names.sessions + '_removed_runs_session')} "
        f"ON {removed_runs} (session_id)",
    )


def run_from_row(run_type: str, run_data: str) -> AnyRunRecord | None:
    """Decode one Agno runs-table row; a row of any other type than agent or team (Agno's workflow runs) reads as None."""
    if run_type not in ("agent", "team"):
        return None
    data = json.loads(run_data)
    _drop_tool_schemas(data)
    run = TeamRunRecord.from_dict(data) if run_type == "team" else RunRecord.from_dict(data)
    split_combined_tool_results(run)
    return run


# LEGACY_COMPAT: Run metadata carrying a copy of every model-visible tool schema.
# Legacy format: Run rows whose `metadata` holds a `tools_schema` list of tool definitions, written with every run,
# including member responses embedded in a paused team run.
# Last legacy release: v2026.10.281; replacement: v2026.10.282 stopped storing tool schemas in run metadata.
# Handling: Every Agno run row the conversion decodes drops the key, from its member responses too, so converted runs
# never hold the schemas; rows written by current releases omit it.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_converted_runs_drop_their_stored_tool_schemas.
def _drop_tool_schemas(data: dict[str, Any]) -> None:
    for run in (data, *(data.get("member_responses") or ())):
        if isinstance(run, dict) and isinstance(metadata := run.get("metadata"), dict):
            metadata.pop("tools_schema", None)


def delete_run_subtrees_in_transaction(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    run_ids: Collection[str],
    *,
    session_id: str,
) -> set[str]:
    """Delete session ``session_id``'s Agno run rows ``run_ids`` and every run below them in the caller's transaction.

    Returns the deleted ids. Team member runs are rows whose ``parent_run_id`` is the team run, so a deleted run
    takes its whole subtree.
    """
    runs = quote_identifier(names.runs)
    found = {run_id for run_id in run_ids if run_id}
    frontier = found
    while frontier:
        frontier = {
            run_id
            for (run_id,) in connection.execute(
                f"SELECT run_id FROM {runs} WHERE session_id = ? AND parent_run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
                (session_id, json.dumps(sorted(frontier))),
            )
        } - found
        found |= frontier
    connection.execute(
        f"DELETE FROM {runs} WHERE session_id = ? AND run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
        (session_id, json.dumps(sorted(found))),
    )
    return found


def _index_sql(table: str, *columns: str) -> str:
    name = quote_identifier(f"idx_{table}_{'_'.join(columns)}")
    return f"CREATE INDEX IF NOT EXISTS {name} ON {quote_identifier(table)} ({', '.join(columns)})"


__all__ = [
    "AGNO_SCHEMA_VERSIONS_TABLE",
    "AgnoTableNames",
    "agno_runs_sql",
    "agno_schema_sql",
    "agno_summary_rows_sql",
    "delete_run_subtrees_in_transaction",
    "run_from_row",
]
