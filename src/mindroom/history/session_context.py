"""Canonical history scope resolution and the conversation store each scope opens."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from mindroom.constants import resolve_session_state_root
from mindroom.history.storage import new_scope_session
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_TEAM_STATE_ROOT_DIRNAME = "teams"

_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: ConversationStore
    session: ConversationSession | None
    session_id: str | None = None
    storage_factory: Callable[[], ConversationStore] | None = None
    session_exists: bool = True


@dataclass(frozen=True)
class BoundTeamScopeContext:
    """Resolved stable owner and scope for one live team run."""

    owner_agent_name: str
    scope: HistoryScope


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the persisted history scope addressed by one live Agno agent, for the history fixtures that still build one."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


def resolve_bound_history_owner(member_names: Sequence[str]) -> str | None:
    """Return the member whose configured storage settings own one bound team run: the first by name."""
    return min((name for name in member_names if name), default=None)


def resolve_bound_team_scope_context(
    *,
    member_names: Sequence[str],
    config: Config,
    team_name: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> BoundTeamScopeContext | None:
    """Resolve the stable owner and scope backing one live team run."""
    owner_agent_name = resolve_bound_history_owner(member_names)
    if owner_agent_name is None:
        return None

    if team_name is not None and team_name in config.teams:
        team_scope_id = team_name
    else:
        team_scope_id = ad_hoc_team_scope_id(
            member_names,
            config.agents,
            requester_user_id=execution_identity.requester_id if execution_identity is not None else None,
        )
    if team_scope_id is None:
        return None
    return BoundTeamScopeContext(
        owner_agent_name=owner_agent_name,
        scope=HistoryScope(kind="team", scope_id=team_scope_id),
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
    if session_id is None or scope is None:
        yield None
        return
    storage_factory = partial(
        create_scope_session_storage,
        agent_name=agent_name,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    storage = storage_factory()
    session = storage.load_session(session_id, scope.kind)
    session_exists = session is not None
    if session is None and create_session_if_missing:
        session = new_scope_session(session_id=session_id, scope_id=scope.scope_id, is_team=scope.kind == "team")
    yield ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
        session_id=session_id,
        storage_factory=storage_factory,
        session_exists=session_exists,
    )


@contextmanager
def open_bound_scope_session_context(
    *,
    member_names: Sequence[str],
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical scope-backed session context for one bound team run."""
    if not member_names and team_name is not None and team_name in config.teams:
        owner_agent_name, scope = team_name, HistoryScope(kind="team", scope_id=team_name)
    else:
        bound_scope = resolve_bound_team_scope_context(
            member_names=member_names,
            config=config,
            team_name=team_name,
            execution_identity=execution_identity,
        )
        if bound_scope is None:
            yield None
            return
        owner_agent_name, scope = bound_scope.owner_agent_name, bound_scope.scope
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


def create_scope_session_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ConversationStore:
    """Open the canonical conversation store of one persisted history scope."""
    if scope.kind == "agent":
        return create_session_storage(agent_name, config, runtime_paths, execution_identity=execution_identity)
    storage_name = _scope_session_storage_name(scope)
    return create_state_storage(
        storage_name,
        resolve_session_state_root(
            _team_scope_state_root(storage_name=storage_name, runtime_paths=runtime_paths),
            runtime_paths,
        ),
        session_table=f"{storage_name}_sessions",
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
