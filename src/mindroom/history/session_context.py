"""Canonical history scope resolution and the conversation store each scope opens."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.constants import resolve_session_state_root
from mindroom.history.storage import new_scope_session
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.session_storage.schema import TableNames

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_TEAM_STATE_ROOT_DIRNAME = "teams"


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope.

    ``session`` is the session the scope's runs load from and save into: the stored one, or a first one the context
    was given (``with_session``) before anything stored it; None while the context holds neither.
    ``session_exists`` tells whether the scope had a stored session when the context was loaded. ``session_id`` is
    the id the context was loaded for, which the scope's first session takes, so a run is saved from the context alone.
    ``storage_factory`` opens the scope's store again, for work that outlives ``storage``'s handle.
    """

    scope: HistoryScope
    storage: ConversationStore
    session: ConversationSession | None
    session_id: str
    storage_factory: Callable[[], ConversationStore]
    session_exists: bool = True

    def first_session(self) -> ConversationSession:
        """Return a new session of this scope under ``session_id``, as the scope's first run starts it."""
        return new_scope_session(session_id=self.session_id, scope=self.scope)

    def stored_or_first_session(self) -> ConversationSession:
        """Return the session a run of this scope is saved into: the stored one, else the scope's first."""
        return self.session if self.session is not None else self.first_session()

    def with_session(self) -> ScopeSessionContext:
        """Return this context holding the session its runs save into (``stored_or_first_session``).

        A run that reads the returned context later loads the session an earlier run was saved into.
        """
        return replace(self, session=self.stored_or_first_session())


def resolve_bound_team_scope(
    *,
    member_names: Sequence[str],
    config: Config,
    team_name: str | None,
    requester_user_id: str | None = None,
) -> HistoryScope:
    """Resolve the history scope backing one live team run: the configured team's, else its members' ad hoc scope.

    ``team_name`` is the configured team's name; None, or a name no configured team has, means an ad hoc team. Every
    team run names a member, as a configured team lists one and an ad hoc team forms from them, so an ad hoc team that
    names none is refused.
    """
    if team_name is not None and team_name in config.teams:
        return HistoryScope(kind="team", scope_id=team_name)
    team_scope_id = ad_hoc_team_scope_id(member_names, config.agents, requester_user_id=requester_user_id)
    if team_scope_id is None:
        msg = "An ad hoc team that names no member has no history scope"
        raise ValueError(msg)
    return HistoryScope(kind="team", scope_id=team_scope_id)


def load_scope_session(
    *,
    scope: HistoryScope,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> ScopeSessionContext:
    """Load the session context of one already-resolved persisted history scope for the current request."""
    storage_factory = partial(
        create_scope_session_storage,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    storage = storage_factory()
    session = storage.load_session(session_id)
    return ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
        session_id=session_id,
        storage_factory=storage_factory,
        session_exists=session is not None,
    )


def load_bound_scope_session(
    *,
    member_names: Sequence[str],
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None,
) -> ScopeSessionContext:
    """Load the canonical scope-backed session context for one bound team run (``resolve_bound_team_scope``)."""
    return load_scope_session(
        scope=resolve_bound_team_scope(
            member_names=member_names,
            config=config,
            team_name=team_name,
            requester_user_id=execution_identity.requester_id if execution_identity is not None else None,
        ),
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    )


def create_scope_session_storage(
    *,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ConversationStore:
    """Open the canonical conversation store of one persisted history scope; an agent scope's id is its agent."""
    if scope.kind == "agent":
        return create_session_storage(scope.scope_id, config, runtime_paths, execution_identity=execution_identity)
    storage_name = TableNames.of_team(scope).storage_name
    return create_state_storage(
        storage_name,
        resolve_session_state_root(
            _team_scope_state_root(storage_name=storage_name, runtime_paths=runtime_paths),
            runtime_paths,
        ),
        scope,
    )


def _team_scope_state_root(*, storage_name: str, runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / _TEAM_STATE_ROOT_DIRNAME / storage_name
