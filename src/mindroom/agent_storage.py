"""Internal storage helpers for agent runtime state."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.db.base import BaseDb, SessionType
from agno.db.sqlite import SqliteDb
from agno.db.utils import deserialize_history_run
from agno.learn import LearningMachine
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import Engine, create_engine, event, select, text

from mindroom import agno_session_persistence_patch
from mindroom.constants import prompt_roles_for_history_storage
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.agent import Agent
    from agno.run.workflow import WorkflowRunOutput
    from agno.session import Session

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_BUSY_TIMEOUT_SECONDS = 30.0
_MIGRATED_DB_FILES: set[str] = set()
_MIGRATION_LOCK = threading.Lock()
logger = get_logger(__name__)

agno_session_persistence_patch.install_patch()

__all__ = [
    "create_session_storage",
    "create_state_storage",
    "get_agent_runtime_state_dbs",
    "get_agent_session",
    "get_team_session",
    "run_session_storage_operation",
]


async def run_session_storage_operation[Result](
    create_storage: Callable[[], BaseDb],
    operation: Callable[[BaseDb], Result],
) -> Result:
    """Run one application-owned synchronous storage operation off-loop and in order."""
    return await agno_session_persistence_patch.run_registered_storage_operation(
        create_storage,
        operation,
    )


def get_agent_runtime_state_dbs(agent: Agent) -> tuple[BaseDb | None, BaseDb | None]:
    """Return the runtime-owned Agno DB handles attached to one agent."""
    history_db = agent.db if isinstance(agent.db, BaseDb) else None
    learning = agent.learning
    learning_db = learning.db if isinstance(learning, LearningMachine) and isinstance(learning.db, BaseDb) else None
    return history_db, learning_db


def create_state_storage(
    storage_name: str,
    state_root: Path,
    *,
    subdir: str,
    session_table: str,
    prompt_roles: frozenset[str] | None = None,
) -> BaseDb:
    """Create persistent Agno state storage from an already-resolved state root."""
    return _create_sqlite_state_storage(
        storage_name=storage_name,
        state_root=state_root,
        subdir=subdir,
        session_table=session_table,
        prompt_roles=prompt_roles,
    )


def _state_engine(db_file: str) -> Engine:
    """Build an engine that waits for state-database locks before failing."""
    return create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": _BUSY_TIMEOUT_SECONDS},
    )


def _create_sqlite_state_storage(
    storage_name: str,
    state_root: Path,
    *,
    subdir: str,
    session_table: str,
    prompt_roles: frozenset[str] | None = None,
) -> SqliteDb:
    """Create a persistent SQLite database from an already-resolved state root."""
    db_dir = state_root / subdir
    db_dir.mkdir(parents=True, exist_ok=True)
    db_file = str(db_dir / f"{storage_name}.db")
    # Both: the engine is what the database is reached through, and the path
    # is what it reports itself as. Handing over an engine alone leaves
    # ``db_file`` empty on a store that is very much file-backed.
    database = _ConversationSqliteDb(
        prompt_roles=prompt_roles or frozenset(),
        session_table=session_table,
        db_file=db_file,
        db_engine=_state_engine(db_file),
    )
    _ensure_current_schema(database, db_file)
    agno_session_persistence_patch._register_sync_session_storage(
        database,
        db_file=db_file,
        session_table=session_table,
    )
    return database


def _ensure_current_schema(database: _ConversationSqliteDb, db_file: str) -> None:
    """Move a 2.x session database onto the Agno 3 runs table, once per process.

    Agno 3.0 stores each run as its own row instead of a JSON blob on the
    session row. Agno's own migration is keyed on the adapter class name and
    would skip this subclass, and its reads keep merging the legacy blob on
    every load while it exists, so the move happens here and the blob is
    dropped once every run it held is accounted for.
    """
    resolved = str(Path(db_file).resolve())
    with _MIGRATION_LOCK:
        if resolved in _MIGRATED_DB_FILES:
            return
        _migrate_legacy_runs(database)
        _MIGRATED_DB_FILES.add(resolved)


def _migrate_legacy_runs(database: _ConversationSqliteDb) -> None:
    session_table = _quote_identifier(database.session_table_name)
    with database.Session() as sess:
        columns = {row[1] for row in sess.execute(text(f"PRAGMA table_info({session_table})"))}
        if "runs" not in columns:
            return
        legacy_rows = sess.execute(
            text(f"SELECT session_id, user_id, runs FROM {session_table} WHERE runs IS NOT NULL"),  # noqa: S608
        ).fetchall()

    for session_id, user_id, raw_runs in legacy_rows:
        legacy_runs = json.loads(raw_runs) if isinstance(raw_runs, str) else raw_runs
        if not isinstance(legacy_runs, list):
            continue
        persisted_run_ids = set(database._persisted_run_ids(session_id))
        run_payloads = [
            cast("dict[str, Any]", run) for run in legacy_runs if isinstance(run, dict) and run.get("run_id")
        ]
        for run_index, run_payload in enumerate(run_payloads):
            if run_payload["run_id"] in persisted_run_ids:
                continue
            database.upsert_run(run=run_payload, session_id=session_id, user_id=user_id, run_index=run_index)
        migrated = len(database._persisted_run_ids(session_id))
        expected = len(run_payloads)
        if migrated < expected:
            logger.warning(
                "agno_legacy_runs_column_kept",
                db_file=database.db_file,
                session_id=session_id,
                legacy_runs=expected,
                migrated_runs=migrated,
            )
            return

    with database.Session() as sess, sess.begin():
        sess.execute(text(f"ALTER TABLE {session_table} DROP COLUMN runs"))
    database._invalidate_table_cache(database.session_table_name)
    logger.info("agno_legacy_runs_column_migrated", db_file=database.db_file, sessions=len(legacy_rows))


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_AGNO_CONNECT_LISTENER_NAME = "_set_sqlite_pragmas"


def _replace_agno_connection_pragmas(engine: Engine) -> None:
    """Keep rollback journaling on state databases despite Agno 3 forcing WAL.

    Agno's ``SqliteDb`` registers a connect listener that switches every
    connection to ``journal_mode=WAL``. MindRoom state databases stay in the
    default rollback mode because WAL is unsafe on network filesystems and
    breaks single-file backups, so that listener is swapped for one that keeps
    only the part MindRoom wants: foreign keys on, so deleting a session row
    cascades to its runs.
    """
    connect_listeners = cast("Any", engine.pool.dispatch).connect.listeners
    listeners = [
        listener for listener in connect_listeners if getattr(listener, "__name__", None) == _AGNO_CONNECT_LISTENER_NAME
    ]
    if len(listeners) != 1:
        msg = f"Expected exactly one Agno {_AGNO_CONNECT_LISTENER_NAME} connect listener, found {len(listeners)}"
        raise RuntimeError(msg)
    event.remove(engine, "connect", listeners[0])

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _connection_record: object) -> None:  # noqa: ANN401
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


def create_session_storage(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> BaseDb:
    """Create persistent session storage for an agent."""
    return _create_agent_session_db(
        agent_name,
        config,
        runtime_paths,
        session_table=f"{agent_name}_sessions",
        execution_identity=execution_identity,
        prompt_roles=prompt_roles_for_history_storage(),
    )


def _create_agent_session_db(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    session_table: str,
    prompt_roles: frozenset[str] | None = None,
) -> BaseDb:
    """Create persistent session storage for one agent."""
    session_state_root = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ).session_state_root
    return create_state_storage(
        storage_name=agent_name,
        state_root=session_state_root,
        subdir="sessions",
        session_table=session_table,
        prompt_roles=prompt_roles,
    )


class _UncachedRunObjects:
    """Stand-in for Agno's shared run-object cache that rebuilds runs on every read.

    Agno 3.0 shares deserialized run objects across reads and treats them as
    immutable. MindRoom edits loaded runs in place (redaction, compaction,
    response-link metadata) before writing them back, so every read must hand
    out its own objects, as reads did before the cache existed.
    """

    def runs_from_rows(self, session_id: str, rows: Sequence[tuple[str, str]]) -> list[Any]:
        del session_id
        runs = (deserialize_history_run(json.loads(run_text)) for _run_id, run_text in rows)
        return [run for run in runs if run is not None]

    def drop_session(self, session_id: str) -> None:
        del session_id


type _PersistedRun = RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any]


class _ConversationSqliteDb(SqliteDb):
    """SQLite session DB with conversation-specific persistence semantics.

    Agno 3.0 writes only the session row from ``upsert_session`` and expects
    callers to persist runs one at a time. MindRoom's history, compaction, and
    redaction paths still rewrite ``session.runs`` and save the whole session,
    so ``upsert_session`` here also reconciles the runs table with the session:
    runs the session no longer holds are deleted and the rest are upserted in
    session order. Reads always load the full run history for the same reason.
    """

    def __init__(
        self,
        *,
        prompt_roles: frozenset[str],
        session_table: str,
        db_file: str,
        db_engine: Engine,
    ) -> None:
        super().__init__(session_table=session_table, db_file=db_file, db_engine=db_engine)
        self._prompt_roles = prompt_roles
        self._run_object_cache = cast("Any", _UncachedRunObjects())
        _replace_agno_connection_pragmas(db_engine)

    def get_session(
        self,
        session_id: str,
        session_type: SessionType | None = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
        runs_limit: int | None = None,
    ) -> Session | dict[str, Any] | None:
        """Read a canonical conversation session without treating its requester as its owner.

        ``runs_limit`` is ignored: MindRoom's history layer works on the full run
        list and a partially loaded session would be reconciled as if the missing
        runs had been removed.
        """
        del user_id, runs_limit
        return super().get_session(
            session_id=session_id,
            session_type=session_type,
            user_id=None,
            deserialize=deserialize,
        )

    def upsert_run(
        self,
        run: _PersistedRun,
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        super().upsert_run(
            run=_run_without_prompt_messages(run, self._prompt_roles),
            session_id=session_id,
            user_id=user_id,
            run_index=run_index,
        )

    def upsert_session(
        self,
        session: Session,
        deserialize: bool | None = True,
    ) -> Session | dict[str, Any] | None:
        sanitized_session = _session_without_prompt_messages(session, self._prompt_roles)
        result = super().upsert_session(sanitized_session, deserialize=deserialize)
        self._reconcile_runs(sanitized_session)
        return result

    def upsert_sessions(
        self,
        sessions: list[Session],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
    ) -> list[Session | dict[str, Any]]:
        sanitized_sessions = [_session_without_prompt_messages(session, self._prompt_roles) for session in sessions]
        result = super().upsert_sessions(
            sanitized_sessions,
            deserialize=deserialize,
            preserve_updated_at=preserve_updated_at,
        )
        for session in sanitized_sessions:
            self._reconcile_runs(session)
        return result

    def _reconcile_runs(self, session: Session) -> None:
        """Make the runs table hold exactly ``session.runs``, in session order."""
        if not isinstance(session, (AgentSession, TeamSession)):
            return
        runs = [run for run in session.runs or [] if isinstance(run, (RunOutput, TeamRunOutput)) and run.run_id]
        wanted_run_ids = {run.run_id for run in runs}
        stale_run_ids = [
            run_id for run_id in self._persisted_run_ids(session.session_id) if run_id not in wanted_run_ids
        ]
        if stale_run_ids:
            self.delete_runs(stale_run_ids)
        for run_index, run in enumerate(runs):
            super().upsert_run(run=run, session_id=session.session_id, user_id=run.user_id, run_index=run_index)

    def _persisted_run_ids(self, session_id: str) -> list[str]:
        runs_table = self._get_table(table_type="runs", create_table_if_not_found=True)
        if runs_table is None:
            return []
        with self.Session() as sess:
            rows = sess.execute(select(runs_table.c.run_id).where(runs_table.c.session_id == session_id)).fetchall()
        return [run_id for (run_id,) in rows]


def _session_without_prompt_messages(session: Session, prompt_roles: frozenset[str]) -> Session:
    if not _session_has_prompt_messages(session, prompt_roles):
        return session
    sanitized_session = deepcopy(session)
    _strip_prompt_messages_from_session(sanitized_session, prompt_roles)
    return sanitized_session


def _session_has_prompt_messages(session: Session, prompt_roles: frozenset[str]) -> bool:
    if not isinstance(session, (AgentSession, TeamSession)) or not session.runs:
        return False
    return any(_run_has_prompt_messages(run, prompt_roles) for run in session.runs)


def _run_has_prompt_messages(run: object, prompt_roles: frozenset[str]) -> bool:
    return (
        isinstance(run, (RunOutput, TeamRunOutput))
        and run.status != RunStatus.paused
        and run.messages is not None
        and any(message.role in prompt_roles for message in run.messages)
    )


def _strip_prompt_messages_from_session(session: Session, prompt_roles: frozenset[str]) -> None:
    if not isinstance(session, (AgentSession, TeamSession)) or not session.runs:
        return
    for run in session.runs:
        if not isinstance(run, (RunOutput, TeamRunOutput)) or run.status == RunStatus.paused or not run.messages:
            continue
        run.messages = [message for message in run.messages if message.role not in prompt_roles]


def _run_without_prompt_messages(run: _PersistedRun, prompt_roles: frozenset[str]) -> _PersistedRun:
    if not isinstance(run, (RunOutput, TeamRunOutput)) or not _run_has_prompt_messages(run, prompt_roles):
        return run
    sanitized_run = deepcopy(run)
    sanitized_run.messages = [message for message in sanitized_run.messages or [] if message.role not in prompt_roles]
    return sanitized_run


def get_agent_session(storage: BaseDb, session_id: str) -> AgentSession | None:
    """Retrieve and deserialize an AgentSession from storage."""
    raw = storage.get_session(session_id, SessionType.AGENT)
    if raw is None:
        return None
    if isinstance(raw, AgentSession):
        return raw
    if isinstance(raw, dict):
        return AgentSession.from_dict(cast("dict[str, Any]", raw))
    return None


def get_team_session(storage: BaseDb, session_id: str) -> TeamSession | None:
    """Retrieve and deserialize a TeamSession from storage."""
    raw = storage.get_session(session_id, SessionType.TEAM)
    if raw is None:
        return None
    if isinstance(raw, TeamSession):
        return raw
    if isinstance(raw, dict):
        return TeamSession.from_dict(cast("dict[str, Any]", raw))
    return None
