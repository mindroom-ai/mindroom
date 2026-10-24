"""Conversation-state persistence helpers for bot flows."""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.entity_resolution import entity_identity_registry
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope
from mindroom.runtime_protocols import SupportsConfig  # noqa: TC001
from mindroom.session_storage.lanes import run_session_storage_operation

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import SessionKind
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

    def team_history_scope(
        self,
        team_agents: list[MatrixID],
        *,
        requester_user_id: str | None = None,
    ) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        config = self.deps.runtime.config
        if self.deps.agent_name in config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        registry = entity_identity_registry(config, self.deps.runtime_paths)
        member_names = [registry.entity_name_or_username(matrix_id) for matrix_id in team_agents]
        scope_id = (
            ad_hoc_team_scope_id(
                member_names,
                config.agents,
                requester_user_id=requester_user_id,
                missing_requester_message="Private ad hoc team history scope requires requester_user_id",
            )
            or "team_"
        )
        return HistoryScope(kind="team", scope_id=scope_id)

    def create_storage(
        self,
        execution_identity: ToolExecutionIdentity | None,
        *,
        scope: HistoryScope | None = None,
    ) -> ConversationStore:
        """Open the conversation store of one exact persisted history scope."""
        return create_scope_session_storage(
            scope=self.history_scope() if scope is None else scope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    async def apersist_response_event_id_in_session_run(
        self,
        *,
        create_storage: Callable[[], ConversationStore],
        session_id: str,
        session_type: SessionKind,
        run_id: str,
        response_event_id: str,
    ) -> None:
        """Persist Matrix response linkage onto the run that produced it, off-loop in the session target's FIFO lane."""

        def persist(storage: ConversationStore) -> None:
            session = storage.load_session(session_id, session_type)
            if session is None:
                return
            run = next((run for run in session.runs if run.run_id == run_id), None)
            if run is None or (run.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY) == response_event_id:
                return
            linked_run = copy(run)
            linked_run.metadata = {**(run.metadata or {}), MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id}
            storage.save_runs(session, [linked_run])

        await run_session_storage_operation(create_storage, persist)
