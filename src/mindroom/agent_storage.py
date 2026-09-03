"""Internal storage helpers for agent runtime state."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from agno.db.base import BaseDb, SessionType
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import Engine, create_engine, event, select

from mindroom import agno_session_persistence_patch
from mindroom.constants import prompt_roles_for_history_storage
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from agno.agent import Agent
    from agno.run.workflow import WorkflowRunOutput
    from agno.session import Session

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_BUSY_TIMEOUT_SECONDS = 30.0
logger = get_logger(__name__)

agno_session_persistence_patch.install_patch()

__all__ = [
    "create_session_storage",
    "create_state_storage",
    "get_agent_runtime_state_dbs",
    "get_agent_session",
    "get_team_session",
    "replace_runs",
    "run_session_storage_operation",
    "runs_without",
    "save_runs",
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
    agno_session_persistence_patch._register_sync_session_storage(
        database,
        db_file=db_file,
        session_table=session_table,
    )
    return database


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
    listeners = [listener for listener in connect_listeners if listener.__name__ == _AGNO_CONNECT_LISTENER_NAME]
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


type _PersistedRun = RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any]


class _ConversationSqliteDb(SqliteDb):
    """SQLite session DB with conversation-specific persistence semantics.

    Agno 3 stores runs as rows of ``<session_table>_runs``: ``upsert_session``
    writes the session row only, ``upsert_run`` writes one run, ``delete_runs``
    removes runs. MindRoom follows that model; :func:`save_runs` and
    :func:`replace_runs` are the two module-level helpers callers use when they
    edit or drop runs of a loaded session. The overrides here only adjust what
    agno already does: prompt-role stripping and append-only indexing in
    ``upsert_run``, a full read in ``get_session``, an owner guard on bulk
    session writes, and an atomic ``delete_runs``.

    A 2.x database keeps its ``runs`` blob column. Agno merges that blob into
    every read; ``delete_runs`` scrubs deleted ids out of it in the same
    transaction as the row delete. Nothing else rewrites the blob.
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

        ``runs_limit`` is ignored: MindRoom's history layer (compaction, replay,
        redaction) reasons over the whole run list.
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
        """Persist one run, letting the database place a new run after the existing ones.

        Agno derives ``run_index`` from the run's position in ``session.runs``.
        MindRoom removes runs from the middle of a session (compaction,
        redaction), after which that position collides with the indexes stored
        rows keep, so new rows take ``MAX(run_index) + 1`` instead and existing
        rows keep their index either way.
        """
        del run_index
        super().upsert_run(
            run=_run_without_prompt_messages(run, self._prompt_roles),
            session_id=session_id,
            user_id=user_id,
            run_index=None,
        )

    def delete_runs(self, run_ids: list[str]) -> None:
        """Delete run rows and scrub the same ids from any 2.x ``runs`` blob, in one transaction.

        Agno deletes the rows, then scrubs the blob in a second best-effort
        transaction whose failures it swallows, and skips both when the runs
        table does not exist yet (the state of a 2.x database nothing has
        appended to). A legacy run that survives the scrub comes back on the
        next read, so a redaction must either remove it everywhere or fail.
        """
        if not run_ids:
            return
        wanted = {run_id for run_id in run_ids if run_id}
        runs_table = self._get_table(table_type="runs")
        sessions_table = self._get_table(table_type="sessions")
        with self.Session() as sess, sess.begin():
            if runs_table is not None:
                # Team member runs are rows whose parent_run_id is the team run; a
                # deleted run takes its whole subtree along, as agno's own
                # session-level delete cascades do.
                frontier = list(wanted)
                while frontier:
                    children = sess.execute(
                        select(runs_table.c.run_id).where(runs_table.c.parent_run_id.in_(frontier)),
                    ).scalars()
                    frontier = [child for child in children if child not in wanted]
                    wanted.update(frontier)
                sess.execute(runs_table.delete().where(runs_table.c.run_id.in_(wanted)))
            if sessions_table is None or "runs" not in sessions_table.c:
                return
            rows = sess.execute(
                select(sessions_table.c.session_id, sessions_table.c.runs).where(sessions_table.c.runs.isnot(None)),
            ).fetchall()
            for session_id, blob in rows:
                legacy_runs = _decode_legacy_runs(blob)
                kept = _dicts_without(legacy_runs, wanted)
                if len(kept) == len(legacy_runs):
                    continue
                sess.execute(
                    sessions_table.update()
                    .where(sessions_table.c.session_id == session_id)
                    .values(runs=json.dumps(kept)),
                )

    def upsert_sessions(
        self,
        sessions: list[Session],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
    ) -> list[Session | dict[str, Any]]:
        """Write sessions one at a time so every row keeps the owner guard.

        Agno's bulk statement updates on conflict without checking the stored
        ``user_id``, so a batch could hand another user's session to a new
        owner. Nothing in MindRoom or Agno's runtime calls this in bulk, so the
        per-row path costs nothing.
        """
        accepted: list[Session | dict[str, Any]] = []
        for session in sessions:
            result = self.upsert_session(session, deserialize=deserialize)
            if result is None:
                continue
            if preserve_updated_at and session.updated_at is not None:
                self._restore_updated_at(session.session_id, session.updated_at)
                if isinstance(result, dict):
                    cast("dict[str, Any]", result)["updated_at"] = session.updated_at
                else:
                    result.updated_at = session.updated_at
            accepted.append(result)
        return accepted

    def _restore_updated_at(self, session_id: str, updated_at: int) -> None:
        """Put back the caller's ``updated_at`` that the single-row upsert stamps with now."""
        sessions_table = self._get_table(table_type="sessions")
        if sessions_table is None:
            return
        with self.Session() as sess, sess.begin():
            sess.execute(
                sessions_table.update().where(sessions_table.c.session_id == session_id).values(updated_at=updated_at),
            )


def _dicts_without(runs: list[Any], run_ids: set[str]) -> list[Any]:
    """Legacy blob entries minus ``run_ids`` and every entry descending from them."""
    removed = set(run_ids)
    while True:
        children = {
            run["run_id"]
            for run in runs
            if isinstance(run, dict) and run.get("parent_run_id") in removed and run.get("run_id") not in removed
        }
        if not children:
            break
        removed |= children
    return [run for run in runs if not (isinstance(run, dict) and run.get("run_id") in removed)]


def runs_without(
    runs: Iterable[RunOutput | TeamRunOutput],
    run_ids: Iterable[str],
) -> list[RunOutput | TeamRunOutput]:
    """Return ``runs`` minus ``run_ids`` and every run descending from them through ``parent_run_id``."""
    run_list = list(runs)
    removed = {run_id for run_id in run_ids if run_id}
    if not removed:
        return run_list
    children_by_parent: dict[str, list[str]] = {}
    for run in run_list:
        if isinstance(run.parent_run_id, str) and run.parent_run_id and isinstance(run.run_id, str) and run.run_id:
            children_by_parent.setdefault(run.parent_run_id, []).append(run.run_id)
    frontier = list(removed)
    while frontier:
        parent = frontier.pop()
        for child in children_by_parent.get(parent, []):
            if child not in removed:
                removed.add(child)
                frontier.append(child)
    return [run for run in run_list if run.run_id not in removed]


def _decode_legacy_runs(blob: object) -> list[Any]:
    """The run dicts inside a 2.x ``runs`` value, or nothing when agno's read merge would ignore it too."""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return []
    return blob if isinstance(blob, list) else []


def save_runs(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    runs: Iterable[RunOutput | TeamRunOutput],
) -> None:
    """Write ``runs`` as rows of ``session`` and make them the session's copies of those runs.

    The session row must already exist (the runs table references it). A run
    already in ``session.runs`` under the same ``run_id`` is replaced by the
    given object, so callers edit a copy and hand the copy here: agno shares
    loaded run objects across reads and treats them as immutable. The rows
    are written first; a failed write leaves ``session.runs`` untouched so a
    retry does not believe the change already landed.
    """
    runs = list(runs)
    if not runs:
        return
    loaded = {id(existing) for existing in session.runs or []}
    if any(id(run) in loaded for run in runs):
        msg = "save_runs received a run object loaded from the session; edit a copy instead"
        raise ValueError(msg)
    for run in runs:
        storage.upsert_run(run=run, session_id=session.session_id, user_id=run.user_id)
    replacements: dict[str, RunOutput | TeamRunOutput] = {run.run_id: run for run in runs if run.run_id}
    merged: list[Any] = []
    for existing in session.runs or []:
        run_id = existing.run_id if isinstance(existing, (RunOutput, TeamRunOutput)) else None
        merged.append(replacements.pop(run_id, existing) if run_id else existing)
    merged.extend(replacements.values())
    session.runs = merged


def replace_runs(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    runs: Iterable[RunOutput | TeamRunOutput],
) -> list[str]:
    """Make ``runs`` the session's run list and delete the rows of the runs it dropped.

    Surviving runs are not rewritten; only removal is persisted, and it is
    persisted before ``session.runs`` changes so a failed delete leaves the
    session as loaded. Returns the removed run ids.
    """
    kept = list(runs)
    kept_ids = {run.run_id for run in kept}
    removed = [
        run.run_id
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.run_id and run.run_id not in kept_ids
    ]
    if removed:
        storage.delete_runs(removed)
    session.runs = kept
    return removed


def _run_has_prompt_messages(run: object, prompt_roles: frozenset[str]) -> bool:
    return (
        isinstance(run, (RunOutput, TeamRunOutput))
        and run.status != RunStatus.paused
        and run.messages is not None
        and any(message.role in prompt_roles for message in run.messages)
    )


def _run_without_prompt_messages(run: _PersistedRun, prompt_roles: frozenset[str]) -> _PersistedRun:
    if not isinstance(run, (RunOutput, TeamRunOutput)) or not _run_has_prompt_messages(run, prompt_roles):
        return run
    sanitized_run = deepcopy(run)
    sanitized_run.messages = [message for message in sanitized_run.messages or [] if message.role not in prompt_roles]
    return sanitized_run


def get_agent_session(storage: BaseDb, session_id: str) -> AgentSession | None:
    """Load one agent session, or None when the row is missing or not an agent session."""
    session = storage.get_session(session_id, SessionType.AGENT)
    return session if isinstance(session, AgentSession) else None


def get_team_session(storage: BaseDb, session_id: str) -> TeamSession | None:
    """Load one team session, or None when the row is missing or not a team session."""
    session = storage.get_session(session_id, SessionType.TEAM)
    return session if isinstance(session, TeamSession) else None
