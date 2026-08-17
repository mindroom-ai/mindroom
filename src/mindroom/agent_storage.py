"""Internal storage helpers for agent runtime state."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from agno.db.base import BaseDb, SessionType
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine
from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import Engine, create_engine

from mindroom.constants import prompt_roles_for_history_storage
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent
    from agno.session import Session

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_BUSY_TIMEOUT_SECONDS = 30.0

__all__ = [
    "create_culture_storage",
    "create_session_storage",
    "create_state_storage",
    "get_agent_runtime_state_dbs",
    "get_agent_session",
    "get_team_session",
]


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
    engine = _state_engine(db_file)
    if prompt_roles is not None:
        return _SessionSanitizingSqliteDb(
            prompt_roles=prompt_roles,
            session_table=session_table,
            db_file=db_file,
            db_engine=engine,
        )
    # Both: the engine is what the database is reached through, and the path
    # is what it reports itself as. Handing over an engine alone leaves
    # ``db_file`` empty on a store that is very much file-backed.
    return SqliteDb(session_table=session_table, db_file=db_file, db_engine=engine)


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


class _SessionSanitizingSqliteDb(SqliteDb):
    """SQLite session DB that strips sensitive nested and prompt payloads."""

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

    def upsert_session(
        self,
        session: Session,
        deserialize: bool | None = True,
    ) -> Session | dict[str, Any] | None:
        return super().upsert_session(
            _session_for_storage(session, self._prompt_roles),
            deserialize=deserialize,
        )

    def upsert_sessions(
        self,
        sessions: list[Session],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
    ) -> list[Session | dict[str, Any]]:
        return super().upsert_sessions(
            [_session_for_storage(session, self._prompt_roles) for session in sessions],
            deserialize=deserialize,
            preserve_updated_at=preserve_updated_at,
        )


def _session_for_storage(session: Session, prompt_roles: frozenset[str]) -> Session:
    if not _session_has_prompt_messages(session, prompt_roles) and not _session_has_member_responses(session):
        return session
    sanitized_session = deepcopy(session)
    _strip_prompt_messages_from_session(sanitized_session, prompt_roles)
    _retain_member_usage_only(sanitized_session)
    return sanitized_session


def _session_has_prompt_messages(session: Session, prompt_roles: frozenset[str]) -> bool:
    if not isinstance(session, (AgentSession, TeamSession)) or not session.runs:
        return False
    return any(
        isinstance(run, (RunOutput, TeamRunOutput))
        and run.status != RunStatus.paused
        and run.messages is not None
        and any(message.role in prompt_roles for message in run.messages)
        for run in session.runs
    )


def _strip_prompt_messages_from_session(session: Session, prompt_roles: frozenset[str]) -> None:
    if not isinstance(session, (AgentSession, TeamSession)) or not session.runs:
        return
    for run in session.runs:
        if not isinstance(run, (RunOutput, TeamRunOutput)) or run.status == RunStatus.paused or not run.messages:
            continue
        run.messages = [message for message in run.messages if message.role not in prompt_roles]


def _session_has_member_responses(session: Session) -> bool:
    if not isinstance(session, TeamSession) or not session.runs:
        return False
    return any(isinstance(run, TeamRunOutput) and bool(run.member_responses) for run in session.runs)


def _retain_member_usage_only(session: Session) -> None:
    if not isinstance(session, TeamSession) or not session.runs:
        return
    for run in session.runs:
        if not isinstance(run, TeamRunOutput):
            continue
        run.member_responses = (
            [] if run.status == RunStatus.paused else [_usage_only_run(member) for member in run.member_responses]
        )


def _usage_only_run(run: RunOutput | TeamRunOutput) -> RunOutput | TeamRunOutput:
    if isinstance(run, TeamRunOutput):
        return TeamRunOutput(
            run_id=run.run_id,
            team_id=run.team_id,
            team_name=run.team_name,
            session_id=run.session_id,
            parent_run_id=run.parent_run_id,
            user_id=run.user_id,
            metrics=_usage_only_metrics(run.metrics),
            model=run.model,
            model_provider=run.model_provider,
            member_responses=[_usage_only_run(member) for member in run.member_responses],
            created_at=run.created_at,
            status=run.status,
        )
    return RunOutput(
        run_id=run.run_id,
        agent_id=run.agent_id,
        agent_name=run.agent_name,
        session_id=run.session_id,
        parent_run_id=run.parent_run_id,
        user_id=run.user_id,
        metrics=_usage_only_metrics(run.metrics),
        model=run.model,
        model_provider=run.model_provider,
        created_at=run.created_at,
        status=run.status,
    )


def _usage_only_metrics(metrics: RunMetrics | None) -> RunMetrics | None:
    if metrics is None:
        return None
    details = (
        {
            model_type: [_usage_only_model_metrics(model_metrics) for model_metrics in model_metrics_list]
            for model_type, model_metrics_list in metrics.details.items()
        }
        if metrics.details
        else None
    )
    return RunMetrics(
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        total_tokens=metrics.total_tokens,
        audio_input_tokens=metrics.audio_input_tokens,
        audio_output_tokens=metrics.audio_output_tokens,
        audio_total_tokens=metrics.audio_total_tokens,
        cache_read_tokens=metrics.cache_read_tokens,
        cache_write_tokens=metrics.cache_write_tokens,
        reasoning_tokens=metrics.reasoning_tokens,
        cost=metrics.cost,
        details=details,
    )


def _usage_only_model_metrics(metrics: ModelMetrics) -> ModelMetrics:
    return ModelMetrics(
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        total_tokens=metrics.total_tokens,
        audio_input_tokens=metrics.audio_input_tokens,
        audio_output_tokens=metrics.audio_output_tokens,
        audio_total_tokens=metrics.audio_total_tokens,
        cache_read_tokens=metrics.cache_read_tokens,
        cache_write_tokens=metrics.cache_write_tokens,
        reasoning_tokens=metrics.reasoning_tokens,
        cost=metrics.cost,
        id=metrics.id,
        provider=metrics.provider,
    )


def create_culture_storage(culture_name: str, storage_path: Path) -> BaseDb:
    """Create persistent culture storage shared by all agents in a culture."""
    culture_dir = storage_path / "culture"
    culture_dir.mkdir(parents=True, exist_ok=True)
    return SqliteDb(db_file=str(culture_dir / f"{culture_name}.db"))


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
