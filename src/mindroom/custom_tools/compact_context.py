"""Agent-controlled context compaction tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agents import _get_agent_session, create_session_storage
from mindroom.compaction import CompactionOutcome, queue_pending_compaction
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.compaction import PendingCompaction
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


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
        self._pending_compaction_buffer = pending_compaction_buffer
        super().__init__(name="compact_context", tools=[self.compact_context])

    async def compact_context(self, keep_recent_runs: int = 2) -> str:
        """Compact older conversation history into a durable summary."""
        if keep_recent_runs < 0:
            return "Error: keep_recent_runs must be >= 0."

        context = get_tool_runtime_context()
        if context is None:
            return "Error: No runtime context available. Cannot determine session."

        # Imported lazily to avoid a heavy matrix/runtime import chain during tool registration.
        from mindroom.ai import get_model_instance  # noqa: PLC0415
        from mindroom.thread_utils import create_session_id  # noqa: PLC0415

        session_id = create_session_id(context.room_id, context.resolved_thread_id)
        storage = create_session_storage(
            self._agent_name,
            self._config,
            self._runtime_paths,
            execution_identity=self._execution_identity,
        )
        session = _get_agent_session(storage, session_id)
        if not session or not session.runs or len(session.runs) <= keep_recent_runs:
            run_count = len(session.runs) if session and session.runs else 0
            return f"Nothing to compact: {run_count} run(s) in session (need more than {keep_recent_runs} to compact)."

        compaction_config = self._config.get_agent_compaction_config(self._agent_name)
        summary_model_name = compaction_config.model or self._config.get_entity_model_name(self._agent_name)
        summary_model = get_model_instance(
            self._config,
            self._runtime_paths,
            summary_model_name,
        )
        # Use compaction model's context window for budget, fall back to agent model
        summary_model_config = self._config.models.get(summary_model_name)
        active_model_name = self._config.get_entity_model_name(self._agent_name)
        active_model_config = self._config.models.get(active_model_name)
        compaction_model_context_window = (
            summary_model_config.context_window
            if summary_model_config and summary_model_config.context_window
            else None
        )
        window_tokens = active_model_config.context_window if active_model_config else None
        if window_tokens is None:
            window_tokens = 0
        threshold_tokens = compaction_config.threshold_tokens
        if threshold_tokens is None and compaction_config.threshold_percent is not None and window_tokens > 0:
            threshold_tokens = int(window_tokens * compaction_config.threshold_percent)
        if threshold_tokens is None:
            threshold_tokens = 0

        try:
            outcome = await queue_pending_compaction(
                storage=storage,
                session_id=session_id,
                agent_name=self._agent_name,
                config=self._config,
                runtime_paths=self._runtime_paths,
                execution_identity=self._execution_identity,
                model=summary_model,
                keep_recent_runs=keep_recent_runs,
                window_tokens=window_tokens,
                threshold_tokens=threshold_tokens,
                reserve_tokens=compaction_config.reserve_tokens,
                notify=compaction_config.notify,
                compaction_model_context_window=compaction_model_context_window,
                pending_buffer=self._pending_compaction_buffer,
            )
        except Exception as exc:
            logger.exception(
                "Failed to queue manual compaction",
                agent=self._agent_name,
                session_id=session_id,
                error=str(exc),
            )
            return f"Failed to compact context: {exc}"

        if outcome is None:
            return (
                "Compaction failed: no runs fit the compaction model's input budget. "
                "This may happen when the previous summary is too large or all candidate "
                "runs exceed the available budget. Consider increasing the compaction model's "
                "context window or resetting the session."
            )
        return _format_outcome(outcome)


def _format_outcome(outcome: CompactionOutcome) -> str:
    reduction_pct = 0
    if outcome.before_tokens > 0:
        reduction_pct = int((1 - (outcome.after_tokens / outcome.before_tokens)) * 100)

    topics_line = ""
    if outcome.topics:
        topics_line = f"\n- Topics preserved: {', '.join(outcome.topics)}"

    return (
        "Compaction queued:\n"
        f"- Runs: {outcome.runs_before} -> {outcome.runs_after}\n"
        f"- Tokens: ~{outcome.before_tokens:,} -> ~{outcome.after_tokens:,} "
        f"({reduction_pct}% reduction)\n"
        "- Status: Will apply after this response finishes."
        f"{topics_line}"
    )
