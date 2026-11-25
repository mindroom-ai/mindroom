"""Agent-controlled context compaction tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.main import Config  # noqa: TC001
from mindroom.constants import RuntimePaths  # noqa: TC001
from mindroom.history.manual import resolve_manual_compaction_plan, schedule_compaction_before_next_reply
from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


class CompactContextTools(Toolkit):
    """Tool that triggers scoped mid-turn context compaction."""

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

    async def compact_context(self) -> str:
        """Compact conversation context now before the next model request."""
        runtime_context = get_tool_runtime_context()
        # The scope the running entity stores to: a team member's is its team's, an agent's own is its own.
        call = current_tool_call()
        scope = (
            call.context.history_scope
            if call is not None and call.context.history_scope is not None
            else HistoryScope(kind="agent", scope_id=self._agent_name)
        )
        # The session the toolkit was built for, else the running conversation's.
        built_session_id = self._execution_identity.session_id if self._execution_identity is not None else None
        session_id = built_session_id or (runtime_context.session_id if runtime_context is not None else None)
        if session_id is None:
            return "Error: No active session available. Cannot determine session."
        # A team member's call carries the leader's model on call.context.model_name while runtime_context is rebound
        # to the member's own model, so prefer call.context.model_name for the scope being compacted.
        active_model_name = (
            call.context.model_name
            if call is not None and call.context.model_name is not None
            else (runtime_context.active_model_name if runtime_context is not None else None)
        )
        room_id = runtime_context.room_id if runtime_context is not None else None
        thread_id = runtime_context.resolved_thread_id if runtime_context is not None else None

        compaction_requests = call.context.compaction_requests if call is not None else None
        if compaction_requests is not None:
            plan = resolve_manual_compaction_plan(
                scope,
                self._config,
                self._runtime_paths,
                room_id,
                thread_id,
                None,
                active_model_name,
            )
            if isinstance(plan, str):
                return f"Error: Compaction is unavailable for this scope because {plan}."
            compaction_requests.add(scope)
            logger.info("Mid-turn compaction requested", agent=self._agent_name, scope=scope.key)
            return "Compaction will run now before the next model request in this scope."

        unavailable = await schedule_compaction_before_next_reply(
            scope=scope,
            agent_name=self._agent_name,
            session_id=session_id,
            runtime_paths=self._runtime_paths,
            config=self._config,
            execution_identity=self._execution_identity,
            active_model_name=active_model_name,
            room_id=room_id,
            thread_id=thread_id,
        )
        if unavailable is not None:
            return f"Error: Compaction is unavailable for this scope because {unavailable}."
        return "Compaction will run before the next reply in this conversation scope."
