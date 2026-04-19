"""Agent-controlled context compaction tool."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agno.agent import Agent  # noqa: TC002
from agno.run import RunContext  # noqa: TC002
from agno.tools import Toolkit

from mindroom.config.main import Config, ResolvedRuntimeModel  # noqa: TC001
from mindroom.config.models import CompactionConfig  # noqa: TC001
from mindroom.constants import RuntimePaths  # noqa: TC001
from mindroom.history.policy import manual_compaction_unavailable_message, resolve_history_execution_plan
from mindroom.history.runtime import open_scope_session_context
from mindroom.history.storage import add_pending_force_compaction_scope, read_scope_state, write_scope_state
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    resolve_current_session_id,
)

if TYPE_CHECKING:
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

    async def compact_context(self, agent: Agent | None = None, run_context: RunContext | None = None) -> str:
        """Request compaction before the next run in the current scope."""
        if agent is None:
            return "Error: No active agent available. Cannot determine history scope."

        runtime_context = get_tool_runtime_context()
        session_id = resolve_current_session_id(
            execution_identity=self._execution_identity,
            runtime_context=runtime_context,
        )
        if session_id is None:
            return "Error: No active session available. Cannot determine session."

        with open_scope_session_context(
            agent=agent,
            agent_name=self._agent_name,
            session_id=session_id,
            runtime_paths=self._runtime_paths,
            config=self._config,
            execution_identity=self._execution_identity,
            create_session_if_missing=True,
        ) as scope_context:
            if scope_context is None:
                return "Error: Current agent has no history scope. Cannot compact context."
            if scope_context.session is None:
                return "Error: No stored session available. Cannot compact context."

            runtime_model, compaction_config = self._resolve_active_compaction_settings(agent, runtime_context)
            budget_error = self._validate_compaction_budget(
                active_model_name=runtime_model.model_name,
                active_context_window=runtime_model.context_window,
                compaction_config=compaction_config,
            )
            if budget_error is not None:
                return budget_error

            session = scope_context.session
            current_state = read_scope_state(session, scope_context.scope)
            next_state = replace(current_state, force_compact_before_next_run=True)
            write_scope_state(session, scope_context.scope, next_state)
            scope_context.storage.upsert_session(session)
            if run_context is not None:
                run_context.session_state = add_pending_force_compaction_scope(
                    run_context.session_state,
                    scope_context.scope,
                )
            logger.info(
                "Manual compaction scheduled",
                agent=self._agent_name,
                scope=scope_context.scope.key,
            )
            return "Compaction scheduled for the next reply in this conversation scope."

    def _resolve_active_compaction_settings(
        self,
        agent: Agent,
        runtime_context: ToolRuntimeContext | None,
    ) -> tuple[ResolvedRuntimeModel, CompactionConfig]:
        """Resolve the active model and compaction config for the current scope."""
        active_model_name = runtime_context.active_model_name if runtime_context is not None else None
        if agent.team_id is None:
            runtime_model = self._config.resolve_runtime_model(
                entity_name=self._agent_name,
                active_model_name=active_model_name,
                room_id=runtime_context.room_id if runtime_context is not None else None,
                runtime_paths=self._runtime_paths if runtime_context is not None else None,
            )
            return runtime_model, self._config.get_entity_compaction_config(self._agent_name)

        if agent.team_id not in self._config.teams:
            runtime_model = self._config.resolve_runtime_model(
                entity_name=None,
                active_model_name=active_model_name,
            )
            return runtime_model, self._config.get_default_compaction_config()

        runtime_model = self._config.resolve_runtime_model(
            entity_name=agent.team_id,
            active_model_name=active_model_name,
            room_id=runtime_context.room_id if runtime_context is not None else None,
            runtime_paths=self._runtime_paths,
        )
        return runtime_model, self._config.get_entity_compaction_config(agent.team_id)

    def _validate_compaction_budget(
        self,
        *,
        active_model_name: str,
        active_context_window: int | None,
        compaction_config: CompactionConfig,
    ) -> str | None:
        """Return a user-facing error when destructive compaction is unavailable."""
        execution_plan = resolve_history_execution_plan(
            config=self._config,
            compaction_config=compaction_config,
            has_authored_compaction_config=True,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            static_prompt_tokens=None,
        )
        return manual_compaction_unavailable_message(execution_plan)
