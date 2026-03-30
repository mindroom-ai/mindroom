"""Agent-controlled context compaction tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agents import create_session_storage, get_agent_session
from mindroom.history.replay import resolve_history_scope
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.types import CompactionState
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

        scope = resolve_history_scope(agent)
        if scope is None:
            return "Error: Current agent has no replay scope. Cannot compact context."

        storage = create_session_storage(
            self._agent_name,
            self._config,
            self._runtime_paths,
            execution_identity=self._execution_identity,
        )
        session = get_agent_session(storage, session_id)
        if session is None:
            return "Error: No stored session available. Cannot compact context."

        current_state = read_scope_state(session, scope)
        next_state = CompactionState(
            summary=current_state.summary,
            last_compacted_run_id=current_state.last_compacted_run_id,
            compacted_at=current_state.compacted_at,
            summary_model=current_state.summary_model,
            force_compact_before_next_run=True,
        )
        write_scope_state(session, scope, next_state)
        storage.upsert_session(session)
        logger.info(
            "Scheduled scoped compaction for next run",
            agent=self._agent_name,
            session_id=session_id,
            scope=scope.key,
        )
        return "Compaction scheduled for the next reply in this conversation scope."
