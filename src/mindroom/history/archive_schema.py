"""Table layout of the compaction archive, shared by its owner and its legacy migration.

It stays free of storage and runtime imports so ``agent_storage`` can run the
migration when it opens a conversation database.
"""

from __future__ import annotations

from mindroom.usage_storage import quote_identifier


def archive_table_names(session_table: str) -> tuple[str, str]:
    """Return the unquoted generation and archived-run table names beside ``session_table``."""
    return f"{session_table}_compactions", f"{session_table}_compacted_runs"


def archive_schema_sql(session_table: str) -> tuple[str, ...]:
    """Return the idempotent DDL that creates the archive beside ``session_table``."""
    compactions, compacted_runs = (quote_identifier(name) for name in archive_table_names(session_table))
    sessions = quote_identifier(session_table)
    return (
        f"CREATE TABLE IF NOT EXISTS {compactions} (id INTEGER PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {sessions}(session_id) ON DELETE CASCADE, "
        "scope_key TEXT NOT NULL, summary TEXT, summary_model TEXT, "
        "legacy INTEGER NOT NULL DEFAULT 0, legacy_event_ids TEXT, created_at INTEGER NOT NULL)",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(session_table + '_compactions_scope')} "
        f"ON {compactions} (session_id, scope_key)",
        # ``event_ids`` precedes the large ``run_data`` so event scans skip its overflow pages.
        f"CREATE TABLE IF NOT EXISTS {compacted_runs} (id INTEGER PRIMARY KEY, "
        f"compaction_id INTEGER NOT NULL REFERENCES {compactions}(id) ON DELETE CASCADE, "
        "session_id TEXT NOT NULL, run_id TEXT NOT NULL, run_type TEXT, event_ids TEXT NOT NULL, "
        "run_data TEXT, UNIQUE(session_id, run_id))",
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(session_table + '_compacted_runs_generation')} "
        f"ON {compacted_runs} (compaction_id)",
    )
