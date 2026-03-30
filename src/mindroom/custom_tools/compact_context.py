"""Agent-controlled context compaction tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agents import create_session_storage, get_agent_session
from mindroom.compaction import (
    CompactionOutcome,
    get_agent_replay_scope,
    queue_pending_compaction,
    resolve_replay_state,
)
from mindroom.compaction_runtime import (
    normalize_compaction_budget_tokens,
    resolve_compaction_model,
    resolve_effective_compaction_threshold,
)
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_current_session_id

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb

    from mindroom.compaction import CompactionScope, PendingCompaction
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ManualCompactionRequest:
    """Resolved request state for one manual compaction tool call."""

    storage: SqliteDb
    session_id: str
    replay_scope: CompactionScope
    visible_run_count: int


class CompactContextTools(Toolkit):
    """Tool that lets an agent compact older conversation history on demand."""

    def __init__(
        self,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
        pending_compaction_buffer: list[PendingCompaction] | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._runtime_paths = runtime_paths
        self._execution_identity = execution_identity
        self._pending_compaction_buffer = pending_compaction_buffer if pending_compaction_buffer is not None else []
        super().__init__(name="compact_context", tools=[self.compact_context])

    def _resolve_manual_compaction_request(
        self,
        keep_recent_runs: int,
        agent: Agent | None,
    ) -> _ManualCompactionRequest | str:
        """Resolve the current scoped session state for one manual compaction request."""
        if keep_recent_runs < 0:
            return "Error: keep_recent_runs must be >= 0."
        if agent is None:
            return "Error: No active agent available. Cannot determine replay scope."

        session_id = resolve_current_session_id(
            execution_identity=self._execution_identity,
            runtime_context=get_tool_runtime_context(),
        )
        if session_id is None:
            return "Error: No active session available. Cannot determine session."

        storage = create_session_storage(
            self._agent_name,
            self._config,
            self._runtime_paths,
            execution_identity=self._execution_identity,
        )
        replay_scope = get_agent_replay_scope(agent)
        if replay_scope is None:
            return "Error: Current agent has no replay scope. Cannot compact context."

        session = get_agent_session(storage, session_id)
        visible_run_count = len(resolve_replay_state(session, scope=replay_scope).visible_runs) if session else 0
        if visible_run_count <= keep_recent_runs:
            return (
                "Nothing to compact: "
                f"{visible_run_count} visible run(s) in session "
                f"(need more than {keep_recent_runs} to compact)."
            )

        return _ManualCompactionRequest(
            storage=storage,
            session_id=session_id,
            replay_scope=replay_scope,
            visible_run_count=visible_run_count,
        )

    async def compact_context(self, keep_recent_runs: int = 2, agent: Agent | None = None) -> str:
        """Compact older conversation history into a durable summary."""
        request = self._resolve_manual_compaction_request(keep_recent_runs, agent)
        if isinstance(request, str):
            return request

        compaction_config = self._config.get_agent_compaction_config(self._agent_name)
        summary_model, compaction_model_context_window = resolve_compaction_model(
            self._config,
            self._runtime_paths,
            self._agent_name,
            compaction_config,
        )
        active_model_name = self._config.get_entity_model_name(self._agent_name)
        active_model_config = self._config.models.get(active_model_name)
        window_tokens = (
            active_model_config.context_window if active_model_config and active_model_config.context_window else 0
        )
        threshold_tokens = resolve_effective_compaction_threshold(compaction_config, window_tokens)
        reserve_tokens = normalize_compaction_budget_tokens(
            compaction_config.reserve_tokens,
            compaction_model_context_window if compaction_model_context_window is not None else window_tokens or None,
        )

        try:
            outcome = await queue_pending_compaction(
                storage=request.storage,
                session_id=request.session_id,
                agent_name=self._agent_name,
                config=self._config,
                runtime_paths=self._runtime_paths,
                execution_identity=self._execution_identity,
                model=summary_model,
                keep_recent_runs=keep_recent_runs,
                window_tokens=window_tokens,
                threshold_tokens=threshold_tokens,
                reserve_tokens=reserve_tokens,
                notify=compaction_config.notify,
                scope=request.replay_scope,
                compaction_model_context_window=compaction_model_context_window,
                pending_buffer=self._pending_compaction_buffer,
            )
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            logger.exception(
                "Failed to queue manual compaction",
                agent=self._agent_name,
                session_id=request.session_id,
                error=str(exc),
            )
            return f"Failed to compact context: {exc}"

        if outcome is None:
            message = (
                "Compaction failed: no runs fit the compaction model's input budget. "
                "This may happen when the previous summary is too large or all candidate "
                "runs exceed the available budget. Consider increasing the compaction model's "
                "context window or resetting the session."
            )
        else:
            message = _format_outcome(outcome)
        return message


def _format_outcome(outcome: CompactionOutcome) -> str:
    reduction_pct = 0
    if outcome.before_tokens > 0:
        reduction_pct = int((1 - (outcome.after_tokens / outcome.before_tokens)) * 100)

    return (
        "Compaction queued:\n"
        f"- Visible runs: {outcome.runs_before} -> {outcome.runs_after}\n"
        f"- Tokens: ~{outcome.before_tokens:,} -> ~{outcome.after_tokens:,} "
        f"({reduction_pct}% reduction)\n"
        "- Status: Will apply after this response finishes."
    )
