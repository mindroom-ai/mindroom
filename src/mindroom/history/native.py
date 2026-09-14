"""Configure native provider replay under the existing history policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.native_compaction import NativeCompactionModel

if TYPE_CHECKING:
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.types import ResolvedHistoryExecutionPlan, ResolvedHistorySettings


def configure_native_history(
    model: object,
    *,
    plan: ResolvedHistoryExecutionPlan,
    history_settings: ResolvedHistorySettings,
    session: AgentSession | TeamSession | None,
    allowed: bool,
) -> NativeCompactionModel | None:
    """Enable native requests only when they preserve the scope's authored semantics."""
    if not isinstance(model, NativeCompactionModel):
        return None
    threshold = plan.trigger_threshold_tokens
    static_tokens = plan.static_prompt_tokens
    hard_budget = plan.hard_replay_budget_tokens
    enabled = (
        allowed
        and plan.authored_compaction_enabled
        and not plan.explicit_compaction_model
        and history_settings.policy.mode == "all"
        and history_settings.max_tool_calls_from_history is None
        and threshold is not None
        and static_tokens is not None
        and hard_budget is not None
        and static_tokens < threshold < static_tokens + hard_budget
    )
    summary = session.summary.summary.strip() if session is not None and session.summary is not None else ""
    model.configure_native_compaction(
        threshold=threshold if enabled else None,
        history_generation=summary,
    )
    return model


def native_history_route(model: NativeCompactionModel | None) -> str | None:
    """Return the active projection identity, or canonical history when disabled."""
    return model.native_compaction.route if model is not None and model.native_compaction is not None else None
