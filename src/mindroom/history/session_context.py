"""Canonical history scope resolution and the conversation store each scope opens."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
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

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_TEAM_STATE_ROOT_DIRNAME = "teams"

_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope.

    ``session`` is the session the scope's runs load from and save into: the stored one, or a first one the context
    was given (``with_session``) before anything stored it; None while the context holds neither.
    ``session_exists`` tells whether the scope had a stored session when the context was opened. ``session_id`` is
    the id the context was opened for, which the scope's first session takes, so a run is saved from the context alone.
    """

    scope: HistoryScope
    storage: ConversationStore
    session: ConversationSession | None
    session_id: str
    storage_factory: Callable[[], ConversationStore] | None = None
    session_exists: bool = True

    def first_session(self) -> ConversationSession:
        """Return a new session of this scope under ``session_id``, as the scope's first run starts it."""
        return new_scope_session(
            session_id=self.session_id,
            scope_id=self.scope.scope_id,
            is_team=self.scope.kind == "team",
        )

    def stored_or_first_session(self) -> ConversationSession:
        """Return the session a run of this scope is saved into: the stored one, else the scope's first."""
        return self.session if self.session is not None else self.first_session()

    def with_session(self) -> ScopeSessionContext:
        """Return this context holding the session its runs save into (``stored_or_first_session``).

        A run that reads the returned context later loads the session an earlier run was saved into.
        """
        return replace(self, session=self.stored_or_first_session())


@dataclass(frozen=True)
class _BoundTeamScopeContext:
    """Resolved stable owner and scope for one live team run."""

    owner_agent_name: str
    scope: HistoryScope


def resolve_bound_history_owner(member_names: Sequence[str]) -> str | None:
    """Return the member that labels one bound team run's history preparation: the first by name."""
    # TODO(simplify): A team run resolves an owner, its first member by name, that team stores ignore and that only
    # labels the run's history-preparation logs. Resolve the team's HistoryScope alone and log the scope. Operators
    # reading those logs would notice.
    return min((name for name in member_names if name), default=None)


def resolve_bound_team_scope_context(
    *,
    member_names: Sequence[str],
    config: Config,
    team_name: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> _BoundTeamScopeContext | None:
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
    return _BoundTeamScopeContext(
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
    context = ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
        session_id=session_id,
        storage_factory=storage_factory,
        session_exists=session is not None,
    )
    yield context.with_session() if create_session_if_missing else context


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
    normalized_scope_id = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
    storage_name = f"team_{normalized_scope_id}_{hashlib.sha256(scope.key.encode()).hexdigest()[:12]}"
    return create_state_storage(
        storage_name,
        resolve_session_state_root(
            _team_scope_state_root(storage_name=storage_name, runtime_paths=runtime_paths),
            runtime_paths,
        ),
    )


def _team_scope_state_root(*, storage_name: str, runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / _TEAM_STATE_ROOT_DIRNAME / storage_name
