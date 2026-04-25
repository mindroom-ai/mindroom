"""Internal storage helpers for agent runtime state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "create_session_storage",
    "create_state_storage_db",
    "get_agent_runtime_sqlite_dbs",
    "get_agent_session",
    "get_team_session",
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


def create_session_storage(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> SqliteDb:
    """Create persistent session storage for an agent."""
    return _create_agent_state_db(
        agent_name,
        config,
        runtime_paths,
        subdir="sessions",
        session_table=f"{agent_name}_sessions",
        execution_identity=execution_identity,
    )


def _create_agent_state_db(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    subdir: str,
    session_table: str,
) -> SqliteDb:
    """Create a persistent SQLite database for one agent state category."""
    state_storage_path = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ).state_root
    return create_state_storage_db(
        storage_name=agent_name,
        state_root=state_storage_path,
        subdir=subdir,
        session_table=session_table,
    )


def get_agent_session(storage: SqliteDb, session_id: str) -> AgentSession | None:
    """Retrieve and deserialize an AgentSession from storage."""
    raw = storage.get_session(session_id, SessionType.AGENT)
    if raw is None:
        return None
    if isinstance(raw, AgentSession):
        return raw
    if isinstance(raw, dict):
        return AgentSession.from_dict(cast("dict[str, Any]", raw))
    return None


def get_team_session(storage: SqliteDb, session_id: str) -> TeamSession | None:
    """Retrieve and deserialize a TeamSession from storage."""
    raw = storage.get_session(session_id, SessionType.TEAM)
    if raw is None:
        return None
    if isinstance(raw, TeamSession):
        return raw
    if isinstance(raw, dict):
        return TeamSession.from_dict(cast("dict[str, Any]", raw))
    return None
