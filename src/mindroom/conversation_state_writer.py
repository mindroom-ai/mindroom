"""Conversation-state persistence helpers for bot flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.agent_storage import (
    create_session_storage,
    get_agent_session,
    get_team_session,
)
from mindroom.history import HistoryScope, create_scope_session_storage
from mindroom.runtime_protocols import SupportsConfig  # noqa: TC001

if TYPE_CHECKING:
    import structlog
    from agno.db.sqlite import SqliteDb

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ConversationStateWriterDeps:
    """Static collaborators for conversation-state persistence and cache writes."""

    runtime: SupportsConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str


@dataclass
class ConversationStateWriter:
    """Own the persisted conversation state for one bot."""

    deps: ConversationStateWriterDeps

    def history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.deps.agent_name in self.deps.runtime.config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        return HistoryScope(kind="agent", scope_id=self.deps.agent_name)

    def session_type_for_scope(self, scope: HistoryScope) -> SessionType:
        """Return the Agno session type used by one persisted history scope."""
        return SessionType.TEAM if scope.kind == "team" else SessionType.AGENT

    def team_history_scope(self, team_agents: list[MatrixID]) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        config = self.deps.runtime.config
        if self.deps.agent_name in config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        team_member_names = [
            matrix_id.agent_name(config, self.deps.runtime_paths) or matrix_id.username for matrix_id in team_agents
        ]
        return HistoryScope(kind="team", scope_id=f"team_{'+'.join(sorted(team_member_names))}")

    def create_storage(
        self,
        execution_identity: ToolExecutionIdentity | None,
        *,
        scope: HistoryScope | None = None,
    ) -> SqliteDb:
        """Create storage for one exact persisted history scope."""
        config = self.deps.runtime.config
        normalized_scope = (
            self.history_scope() if scope is None else HistoryScope(kind=scope.kind, scope_id=scope.scope_id)
        )
        if (
            normalized_scope == self.history_scope()
            and self.session_type_for_scope(normalized_scope) is SessionType.AGENT
        ):
            return create_session_storage(
                agent_name=self.deps.agent_name,
                config=config,
                runtime_paths=self.deps.runtime_paths,
                execution_identity=execution_identity,
            )
        return create_scope_session_storage(
            agent_name=normalized_scope.scope_id if normalized_scope.kind == "agent" else self.deps.agent_name,
            scope=normalized_scope,
            config=config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    def persist_response_event_id_in_session_run(
        self,
        *,
        storage: SqliteDb,
        session_id: str,
        session_type: SessionType,
        run_id: str,
        response_event_id: str,
    ) -> None:
        """Persist Matrix response linkage onto the run that produced it."""
        session = (
            get_team_session(storage, session_id)
            if session_type is SessionType.TEAM
            else get_agent_session(storage, session_id)
        )
        if session is None or not session.runs:
            return
        for run in session.runs:
            if not isinstance(run, (RunOutput, TeamRunOutput)) or run.run_id != run_id:
                continue
            metadata = dict(run.metadata or {})
            if metadata.get("matrix_response_event_id") == response_event_id:
                return
            metadata["matrix_response_event_id"] = response_event_id
            run.metadata = metadata
            storage.upsert_session(session)
            return
