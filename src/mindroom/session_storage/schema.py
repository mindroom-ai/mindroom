"""Table names and layout of a conversation database.

The sessions and runs tables keep the layout Agno 3 created, column for column and index for index, so
databases written before and after Agno's removal are the same files. The summary-row, removed-run, and
usage tables are MindRoom's own; their DDL lives with their owners.
"""

from __future__ import annotations

from dataclasses import dataclass

from mindroom.history.summary_rows_schema import summary_rows_schema_sql, summary_rows_table_names
from mindroom.usage.storage import quote_identifier, usage_table_sql

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
class TableNames:
    """The tables of one conversation database, all named after its sessions table."""

    sessions: str

    @property
    def runs(self) -> str:
        """The runs table."""
        # Agno 3 names the runs table beside its default sessions table ``agno_runs`` (agno/db/base.py, BaseDb.__init__).
        return "agno_runs" if self.sessions == "agno_sessions" else f"{self.sessions}_runs"

    @property
    def summaries(self) -> str:
        """The summary-row table."""
        return summary_rows_table_names(self.sessions)[0]

    @property
    def removed_runs(self) -> str:
        """The table of run ids a cut removed."""
        return summary_rows_table_names(self.sessions)[1]

    @property
    def usage(self) -> str:
        """The usage ledger."""
        return f"{self.sessions}_usage"


def schema_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL that creates every table and index a conversation database has."""
    return (*run_log_sql(names), *summary_rows_schema_sql(names.sessions), usage_table_sql(names.sessions))


def run_log_sql(names: TableNames) -> tuple[str, ...]:
    """Return the idempotent DDL of the sessions and runs tables alone, in Agno 3's layout.

    Migrations that recognize an older database by a missing summary-row or usage table create only these.
    """
    sessions, runs = quote_identifier(names.sessions), quote_identifier(names.runs)
    return (
        f"CREATE TABLE IF NOT EXISTS {sessions} (session_id VARCHAR NOT NULL, session_type VARCHAR NOT NULL, "
        "agent_id VARCHAR, team_id VARCHAR, workflow_id VARCHAR, user_id VARCHAR, session_data JSON, "
        "agent_data JSON, team_data JSON, workflow_data JSON, metadata JSON, summary JSON, "
        "created_at BIGINT NOT NULL, updated_at BIGINT, PRIMARY KEY (session_id))",
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


def _index_sql(table: str, *columns: str) -> str:
    name = quote_identifier(f"idx_{table}_{'_'.join(columns)}")
    return f"CREATE INDEX IF NOT EXISTS {name} ON {quote_identifier(table)} ({', '.join(columns)})"


__all__ = [
    "TableNames",
    "run_log_sql",
    "schema_sql",
]
