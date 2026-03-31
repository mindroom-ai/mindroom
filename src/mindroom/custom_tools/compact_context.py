"""Agent-controlled context compaction tool."""

# ruff: noqa: TC001, TC002

from __future__ import annotations

from dataclasses import dataclass, replace

from agno.agent import Agent
from agno.run import RunContext
from agno.tools import Toolkit

from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig
from mindroom.constants import RuntimePaths
from mindroom.history.compaction import normalize_compaction_budget_tokens, resolve_compaction_runtime_settings
from mindroom.history.runtime import ScopeSessionContext, load_scope_session_context
from mindroom.history.storage import add_pending_force_compaction_scope, read_scope_state, write_scope_state
from mindroom.logging_config import get_logger
from mindroom.token_budget import compute_compaction_input_budget
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    resolve_current_session_id,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class _CompactionRequest:
    """Resolved request data for scheduling compaction in one scope."""

    session_id: str
    scope_context: ScopeSessionContext
    active_model_name: str
    compaction_config: CompactionConfig


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

        request = self._resolve_compaction_request(agent)
        if isinstance(request, str):
            return request

        budget_error = self._validate_compaction_budget(
            active_model_name=request.active_model_name,
            compaction_config=request.compaction_config,
        )
        if budget_error is not None:
            return budget_error

        session = request.scope_context.session
        assert session is not None
        current_state = read_scope_state(session, request.scope_context.scope)
        next_state = replace(current_state, force_compact_before_next_run=True)
        write_scope_state(session, request.scope_context.scope, next_state)
        request.scope_context.storage.upsert_session(session)
        if run_context is not None:
            run_context.session_state = add_pending_force_compaction_scope(
                run_context.session_state,
                request.scope_context.scope,
            )
        logger.info(
            "Scheduled scoped compaction for next run",
            agent=self._agent_name,
            session_id=request.session_id,
            scope=request.scope_context.scope.key,
        )
        return "Compaction scheduled for the next reply in this conversation scope."

    def _resolve_compaction_request(self, agent: Agent) -> _CompactionRequest | str:
        """Resolve the current session, scope, and active compaction settings."""
        runtime_context = get_tool_runtime_context()
        session_id = resolve_current_session_id(
            execution_identity=self._execution_identity,
            runtime_context=runtime_context,
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
            return "Error: Current agent has no history scope. Cannot compact context."
        if scope_context.session is None:
            return "Error: No stored session available. Cannot compact context."

        active_model_name, compaction_config = self._resolve_active_compaction_settings(agent, runtime_context)
        return _CompactionRequest(
            session_id=session_id,
            scope_context=scope_context,
            active_model_name=active_model_name,
            compaction_config=compaction_config,
        )

    def _resolve_active_compaction_settings(
        self,
        agent: Agent,
        runtime_context: ToolRuntimeContext | None,
    ) -> tuple[str, CompactionConfig]:
        """Resolve the active model and compaction config for the current scope."""
        active_model_name = runtime_context.active_model_name if runtime_context is not None else None
        if agent.team_id is None:
            return (
                active_model_name or self._config.get_entity_model_name(self._agent_name),
                self._config.get_entity_compaction_config(self._agent_name),
            )

        if agent.team_id not in self._config.teams:
            return active_model_name or "default", self._config.get_default_compaction_config()

        if active_model_name is None:
            room_id = runtime_context.room_id if runtime_context is not None else None
            active_model_name = self._config.get_effective_team_model_name(
                agent.team_id,
                room_id,
                self._runtime_paths,
            )
        return active_model_name, self._config.get_entity_compaction_config(agent.team_id)

    def _validate_compaction_budget(
        self,
        *,
        active_model_name: str,
        compaction_config: CompactionConfig,
    ) -> str | None:
        """Return a user-facing error when the active compaction runtime cannot summarize history."""
        active_context_window = self._config.get_model_context_window(active_model_name)
        compaction_runtime = resolve_compaction_runtime_settings(
            config=self._config,
            compaction_config=compaction_config,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
        )
        if compaction_runtime.context_window is None:
            return (
                "Error: Compaction is unavailable for this scope because no context_window is configured on the "
                "active model or the selected compaction model."
            )
        reserve_tokens = normalize_compaction_budget_tokens(
            compaction_config.reserve_tokens,
            compaction_runtime.context_window,
        )
        summary_input_budget = compute_compaction_input_budget(
            compaction_runtime.context_window,
            reserve_tokens=reserve_tokens,
        )
        if summary_input_budget <= 0:
            return (
                "Error: Compaction is unavailable for this scope because the active compaction model leaves no "
                "usable summary input budget after reserve and prompt overhead."
            )
        return None
