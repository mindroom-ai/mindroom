"""Canonical history scope resolution and ownership of session storage handles."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.agent_storage import (
    create_session_storage,
    create_state_storage,
    get_agent_runtime_state_dbs,
    get_agent_session,
    get_team_session,
)
from mindroom.constants import prompt_roles_for_history_storage, resolve_session_state_root
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.team_scope import ad_hoc_team_scope_id
from mindroom.tool_jobs.resources import defer_execution_cleanup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_TEAM_STATE_ROOT_DIRNAME = "teams"

_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: BaseDb
    session: AgentSession | TeamSession | None
    session_id: str | None = None
    storage_factory: Callable[[], BaseDb] | None = None
    session_exists: bool = True


@dataclass(frozen=True)
class BoundTeamScopeContext:
    """Resolved stable owner and scope for one live team run."""

    owner_agent: Agent
    owner_agent_name: str
    scope: HistoryScope


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the persisted history scope addressed by one live agent."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


def resolve_bound_history_owner(agents: list[Agent]) -> tuple[Agent | None, str | None]:
    """Return the canonical storage owner for one bound team run."""
    candidates = [(agent_id, agent) for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not candidates:
        return None, None

    owner_agent_name = min(agent_id for agent_id, _agent in candidates)
    for agent_id, agent in candidates:
        if agent_id == owner_agent_name:
            return agent, owner_agent_name
    return None, None


def resolve_bound_team_scope_context(
    *,
    agents: list[Agent],
    config: Config,
    team_name: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> BoundTeamScopeContext | None:
    """Resolve the stable owner and scope backing one live team run."""
    owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
    if owner_agent is None or owner_agent_name is None:
        return None

    if team_name is not None and team_name in config.teams:
        team_scope_id = team_name
    else:
        team_scope_id = ad_hoc_team_scope_id(
            ad_hoc_team_agent_names(agents),
            config.agents,
            requester_user_id=execution_identity.requester_id if execution_identity is not None else None,
        )
    if team_scope_id is None:
        return None
    scope = HistoryScope(kind="team", scope_id=team_scope_id)
    return BoundTeamScopeContext(
        owner_agent=owner_agent,
        owner_agent_name=owner_agent_name,
        scope=scope,
    )


@contextmanager
def _open_scope_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> Iterator[BaseDb]:
    """Open the canonical storage for one persisted history scope."""
    storage = create_scope_session_storage(
        agent_name=agent_name,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    try:
        yield storage
    finally:
        close_execution_storage(storage)


def _build_scope_session_context(
    *,
    scope: HistoryScope | None,
    session_id: str | None,
    storage: BaseDb,
    storage_factory: Callable[[], BaseDb],
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Build one scope/session context from an already-open storage handle."""
    if session_id is None or scope is None:
        return None

    session = get_team_session(storage, session_id) if scope.kind == "team" else get_agent_session(storage, session_id)
    session_exists = session is not None
    if session is None and create_session_if_missing:
        session = new_scope_session(
            session_id=session_id,
            scope_id=scope.scope_id,
            is_team=scope.kind == "team",
        )
    return ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
        session_id=session_id,
        storage_factory=storage_factory,
        session_exists=session_exists,
    )


@contextmanager
def open_resolved_scope_session_context(
    *,
    agent_name: str,
    scope: HistoryScope | None,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open one already-resolved persisted history scope for the current request."""
    if session_id is None:
        yield None
        return
    if scope is None:
        yield None
        return

    def storage_factory() -> BaseDb:
        return create_scope_session_storage(
            agent_name=agent_name,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )

    with _open_scope_storage(
        agent_name=agent_name,
        scope=scope,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    ) as storage:
        yield _build_scope_session_context(
            scope=scope,
            session_id=session_id,
            storage=storage,
            storage_factory=storage_factory,
            create_session_if_missing=create_session_if_missing,
        )


@contextmanager
def open_scope_session_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical persisted history scope for one live agent."""
    resolved_scope = scope or resolve_history_scope(agent)
    with open_resolved_scope_session_context(
        agent_name=agent_name,
        scope=resolved_scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


@contextmanager
def open_bound_scope_session_context(
    *,
    agents: list[Agent],
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical scope-backed session context for one bound team run."""
    if scope is not None:
        _owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
        if owner_agent_name is None:
            yield None
            return
        with open_resolved_scope_session_context(
            agent_name=owner_agent_name,
            scope=scope,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            create_session_if_missing=create_session_if_missing,
        ) as scope_context:
            yield scope_context
        return
    if not agents and team_name is not None and team_name in config.teams:
        with open_resolved_scope_session_context(
            agent_name=team_name,
            scope=HistoryScope(kind="team", scope_id=team_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            create_session_if_missing=create_session_if_missing,
        ) as scope_context:
            yield scope_context
        return

    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=team_name,
        execution_identity=execution_identity,
    )
    if bound_scope is None:
        yield None
        return
    with open_resolved_scope_session_context(
        agent_name=bound_scope.owner_agent_name,
        scope=bound_scope.scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


def create_scope_session_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> BaseDb:
    """Create the canonical storage for one persisted history scope."""
    if scope.kind == "agent":
        return create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )

    storage_name = _scope_session_storage_name(scope)
    return create_state_storage(
        storage_name=storage_name,
        state_root=resolve_session_state_root(
            _team_scope_state_root(storage_name=storage_name, runtime_paths=runtime_paths),
            runtime_paths,
        ),
        subdir="sessions",
        session_table=f"{storage_name}_sessions",
        prompt_roles=prompt_roles_for_history_storage(),
    )


def close_execution_storage(storage: BaseDb) -> None:
    """Close a concrete DB handle after accepted tool users have settled."""

    async def close() -> None:
        storage.close()

    if not defer_execution_cleanup(close, resource=storage):
        storage.close()


def _close_unique_state_dbs(*storages: BaseDb | None) -> None:
    """Close each distinct state DB handle at most once."""
    seen: set[int] = set()
    for storage in storages:
        if storage is None:
            continue
        storage_id = id(storage)
        if storage_id in seen:
            continue
        seen.add(storage_id)
        close_execution_storage(storage)


def close_agent_runtime_state_dbs(
    agent: Agent | None,
    *,
    shared_scope_storage: BaseDb | None = None,
) -> None:
    """Close one agent's runtime-owned state DB handles except a shared scope storage."""
    if agent is None:
        return
    _close_unique_state_dbs(
        *(storage for storage in get_agent_runtime_state_dbs(agent) if storage is not shared_scope_storage),
    )


def close_team_runtime_state_dbs(
    *,
    agents: list[Agent],
    team_db: BaseDb | None,
    shared_scope_storage: BaseDb | None = None,
) -> None:
    """Close all runtime-owned state DB handles for one team request."""
    _close_unique_state_dbs(
        *(
            storage
            for agent in agents
            for storage in get_agent_runtime_state_dbs(agent)
            if storage is not shared_scope_storage
        ),
        team_db if team_db is not shared_scope_storage else None,
    )


def _scope_session_storage_name(scope: HistoryScope) -> str:
    if scope.kind == "agent":
        return scope.scope_id
    normalized_scope_id = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
    digest = hashlib.sha256(scope.key.encode()).hexdigest()[:12]
    return f"team_{normalized_scope_id}_{digest}"


def _team_scope_state_root(
    *,
    storage_name: str,
    runtime_paths: RuntimePaths,
) -> Path:
    return runtime_paths.storage_root / _TEAM_STATE_ROOT_DIRNAME / storage_name


def ad_hoc_team_agent_names(agents: list[Agent]) -> tuple[str, ...]:
    """Return stable member identities used to resolve an ad hoc team scope."""
    return tuple(agent_id for agent in agents if isinstance((agent_id := agent.id), str) and agent_id)
