"""Table layout of the conversation log's summary rows, shared by storage reads, run writes, and migrations.

A scope's history is its runs table in ``run_index`` order with summary rows
positioned between its runs. Each summary row sorts right after the run whose
``run_index`` it records; replay is the scope's latest summary row plus the runs
after it. Removed run ids keep a run a cut deleted from being written again.

It stays free of storage and runtime imports so ``session_storage`` can use it.
"""

from __future__ import annotations

from mindroom.history.run_visibility import NON_VISIBLE_RUN_STATUSES
from mindroom.usage.storage import quote_identifier

NON_VISIBLE_RUN_STATUS_VALUES = tuple(sorted(status.value for status in NON_VISIBLE_RUN_STATUSES))
_NON_VISIBLE_RUN_STATUS_SQL = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUS_VALUES)


def summary_rows_table_names(session_table: str) -> tuple[str, str]:
    """Return the unquoted summary-row and removed-run table names beside ``session_table``."""
    return f"{session_table}_summaries", f"{session_table}_removed_runs"


def summary_rows_schema_sql(session_table: str) -> tuple[str, ...]:
    """Return the idempotent DDL that creates both tables beside ``session_table``."""
    summaries, removed_runs = (quote_identifier(name) for name in summary_rows_table_names(session_table))
    sessions = quote_identifier(session_table)
    return (
        # The large ``segment`` comes last so scans of the small columns skip its overflow pages.
        f"CREATE TABLE IF NOT EXISTS {summaries} (id INTEGER PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, "
        "scope_key TEXT NOT NULL, after_run_index INTEGER NOT NULL, summary TEXT, summary_model TEXT, "
        "event_ids TEXT NOT NULL DEFAULT '[]', seen_event_ids TEXT NOT NULL DEFAULT '[]', "
        "legacy INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, segment_run_id TEXT, segment TEXT)",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(session_table + '_summaries_scope')} "
        f"ON {summaries} (session_id, scope_key, id)",
        f"CREATE TABLE IF NOT EXISTS {removed_runs} (run_id TEXT PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE)",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(session_table + '_removed_runs_session')} "
        f"ON {removed_runs} (session_id)",
    )


def run_scope_key_sql(alias: str) -> str:
    """Return the SQL expression for a runs-table row's ``HistoryScope.key``."""
    return f"CASE WHEN {alias}.run_type = 'team' THEN 'team:' || {alias}.team_id ELSE 'agent:' || {alias}.agent_id END"


def owner_scope_key(session_type: object, agent_id: object, team_id: object) -> str | None:
    """Return the key of the scope that owns a session row and its replayed summary, if the row names one."""
    if session_type == "team" and isinstance(team_id, str) and team_id:
        return f"team:{team_id}"
    if isinstance(agent_id, str) and agent_id:
        return f"agent:{agent_id}"
    return None


def latest_row_index_sql(summaries: str, alias: str) -> str:
    """Return the SQL subquery for the position of the latest summary row of run ``alias``'s scope."""
    return (
        f"(SELECT row.after_run_index FROM {summaries} AS row WHERE row.session_id = {alias}.session_id "  # noqa: S608
        f"AND row.scope_key = {run_scope_key_sql(alias)} ORDER BY row.id DESC LIMIT 1)"
    )


def loads_sql(alias: str, latest_row_index: str) -> str:
    """Return the SQL condition under which top-level run ``alias`` loads, given its scope's latest row position.

    Paused, cancelled, and errored runs always load, and so does every run of a scope without summary
    rows, whose ``latest_row_index`` is ``NULL``.
    """
    return f"({alias}.status IN ({_NON_VISIBLE_RUN_STATUS_SQL}) OR COALESCE({alias}.run_index > {latest_row_index}, 1))"
