"""Internal storage helpers for agent runtime state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent

__all__ = [
    "create_state_storage_db",
    "get_agent_runtime_sqlite_dbs",
]


def get_agent_runtime_sqlite_dbs(agent: Agent) -> tuple[SqliteDb | None, SqliteDb | None]:
    """Return the runtime-owned SQLite handles attached to one agent."""
    history_db = agent.db if isinstance(agent.db, SqliteDb) else None
    learning = agent.learning
    learning_db = learning.db if isinstance(learning, LearningMachine) and isinstance(learning.db, SqliteDb) else None
    return history_db, learning_db


def create_state_storage_db(
    storage_name: str,
    state_root: Path,
    *,
    subdir: str,
    session_table: str,
) -> SqliteDb:
    """Create a persistent SQLite database from an already-resolved state root."""
    db_dir = state_root / subdir
    db_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(session_table=session_table, db_file=str(db_dir / f"{storage_name}.db"))
