"""Agent-controlled context compaction tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.history.runtime import load_scope_session_context
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.types import HistoryScopeState
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_current_session_id

if TYPE_CHECKING:
    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


class CompactContextTools(Toolkit):
    """Tool that requests scoped compaction before the next run."""

    def __init__(
        self,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._runtime_paths = runtime_paths
        self._execution_identity = execution_identity
        super().__init__(name="compact_context", tools=[self.compact_context])

    async def compact_context(self, agent: Agent | None = None) -> str:
        """Request compaction before the next run in the current scope."""
        if agent is None:
            return "Error: No active agent available. Cannot determine replay scope."

        session_id = resolve_current_session_id(
            execution_identity=self._execution_identity,
            runtime_context=get_tool_runtime_context(),
        )
        if session_id is None:
            return "Error: No active session available. Cannot determine session."

        scope_context = load_scope_session_context(
            agent=agent,
            agent_name=self._agent_name,
            session_id=session_id,
            runtime_paths=self._runtime_paths,
            config=self._config,
            execution_identity=self._execution_identity,
            create_session_if_missing=True,
        )
        if scope_context is None:
            return "Error: Current agent has no replay scope. Cannot compact context."
        if scope_context.session is None:
            return "Error: No stored session available. Cannot compact context."
        current_state = read_scope_state(scope_context.session, scope_context.scope)
        next_state = HistoryScopeState(
            summary=current_state.summary,
            last_compacted_run_id=current_state.last_compacted_run_id,
            compacted_at=current_state.compacted_at,
            summary_model=current_state.summary_model,
            force_compact_before_next_run=True,
        )
        write_scope_state(scope_context.session, scope_context.scope, next_state)
        scope_context.storage.upsert_session(scope_context.session)
        logger.info(
            "Scheduled scoped compaction for next run",
            agent=self._agent_name,
            session_id=session_id,
            scope=scope_context.scope.key,
        )
        return "Compaction scheduled for the next reply in this conversation scope."
