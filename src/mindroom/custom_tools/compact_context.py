"""Agent-controlled context compaction tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.agent import Agent  # noqa: TC002
from agno.run import RunContext  # noqa: TC002
from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001
from mindroom.constants import RuntimePaths  # noqa: TC001
from mindroom.history import request_compaction_before_next_reply
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_current_session_id

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class CompactContextTools(Toolkit):
    """Tool that requests scoped compaction before the next reply."""

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

    async def compact_context(self, agent: Agent | None = None, run_context: RunContext | None = None) -> str:
        """Request compaction before the next reply."""
        if agent is None:
            return "Error: No active agent available. Cannot determine history scope."

        runtime_context = get_tool_runtime_context()
        session_id = resolve_current_session_id(
            execution_identity=self._execution_identity,
            runtime_context=runtime_context,
        )
        if session_id is None:
            return "Error: No active session available. Cannot determine session."

        result = request_compaction_before_next_reply(
            agent=agent,
            agent_name=self._agent_name,
            session_id=session_id,
            runtime_paths=self._runtime_paths,
            config=self._config,
            execution_identity=self._execution_identity,
            active_model_name=runtime_context.active_model_name if runtime_context is not None else None,
            room_id=runtime_context.room_id if runtime_context is not None else None,
            session_state=run_context.session_state if run_context is not None else None,
            record_pending_scope_in_session_state=run_context is not None,
        )
        if run_context is not None and result.session_state is not None:
            run_context.session_state = result.session_state
        return result.message
