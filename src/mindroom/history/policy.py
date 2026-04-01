"""Shared history budgeting and compaction-trigger policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.history.compaction import (
    normalize_compaction_budget_tokens,
    resolve_compaction_runtime_settings,
    resolve_effective_compaction_threshold,
)
from mindroom.history.types import ResolvedHistoryExecutionPlan, _CompactionAvailabilityReason
from mindroom.token_budget import compute_compaction_input_budget

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig


def resolve_history_execution_plan(
    *,
    config: Config,
    compaction_config: CompactionConfig,
    has_authored_compaction_config: bool,
    active_model_name: str,
    active_context_window: int | None,
    static_prompt_tokens: int | None,
) -> ResolvedHistoryExecutionPlan:
    """Resolve all history-budget policy for one run scope in one place."""
    compaction_runtime = resolve_compaction_runtime_settings(
        config=config,
        compaction_config=compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    compaction_context_window = compaction_runtime.context_window
    replay_window_tokens = active_context_window
    summary_input_budget_tokens, unavailable_reason = _resolve_summary_input_budget(
        compaction_context_window=compaction_context_window,
        reserve_tokens=compaction_config.reserve_tokens,
    )

    threshold_tokens = None
    replay_budget_tokens = None
    if replay_window_tokens is not None and static_prompt_tokens is not None:
        threshold_tokens = _resolve_replay_threshold_tokens(
            compaction_config=compaction_config,
            replay_window_tokens=replay_window_tokens,
        )
        replay_budget_tokens = _resolve_replay_budget_tokens(
            compaction_config=compaction_config,
            has_authored_compaction_config=has_authored_compaction_config,
            replay_window_tokens=replay_window_tokens,
            threshold_tokens=threshold_tokens,
            static_prompt_tokens=static_prompt_tokens,
        )

    return ResolvedHistoryExecutionPlan(
        authored_compaction_config=has_authored_compaction_config,
        authored_compaction_enabled=has_authored_compaction_config and compaction_config.enabled,
        destructive_compaction_available=unavailable_reason is None,
        explicit_compaction_model=compaction_config.model is not None,
        compaction_model_name=compaction_runtime.model_name,
        compaction_context_window=compaction_context_window,
        replay_window_tokens=replay_window_tokens,
        trigger_threshold_tokens=threshold_tokens,
        reserve_tokens=compaction_config.reserve_tokens,
        static_prompt_tokens=static_prompt_tokens,
        replay_budget_tokens=replay_budget_tokens,
        summary_input_budget_tokens=summary_input_budget_tokens,
        unavailable_reason=unavailable_reason,
    )


def should_attempt_destructive_compaction(
    *,
    plan: ResolvedHistoryExecutionPlan,
    force_compact_before_next_run: bool,
    current_history_tokens: int | None,
    replay_budget_tokens: int | None = None,
) -> bool:
    """Return whether durable session compaction should run before replay planning."""
    if force_compact_before_next_run and plan.destructive_compaction_available:
        return True

    if not plan.authored_compaction_enabled or not plan.destructive_compaction_available:
        return False

    effective_replay_budget_tokens = plan.replay_budget_tokens if replay_budget_tokens is None else replay_budget_tokens
    if effective_replay_budget_tokens is None or current_history_tokens is None:
        return False

    return current_history_tokens > effective_replay_budget_tokens


def manual_compaction_unavailable_message(plan: ResolvedHistoryExecutionPlan) -> str | None:
    """Return the user-facing error for an unavailable manual compaction request."""
    description = describe_compaction_unavailability(plan)
    if description is None:
        return None
    return f"Error: Compaction is unavailable for this scope because {description}."


def describe_compaction_unavailability(plan: ResolvedHistoryExecutionPlan) -> str | None:
    """Return a short description for one unavailable destructive-compaction reason."""
    reason = plan.unavailable_reason
    if reason == "no_context_window":
        if plan.explicit_compaction_model:
            return "no context_window is configured on the selected compaction model"
        return "no context_window is configured on the active model"
    if reason == "non_positive_summary_input_budget":
        return "the active compaction model leaves no usable summary input budget after reserve and prompt overhead"
    return None


def _resolve_summary_input_budget(
    *,
    compaction_context_window: int | None,
    reserve_tokens: int,
) -> tuple[int | None, _CompactionAvailabilityReason | None]:
    if compaction_context_window is None:
        return None, "no_context_window"

    normalized_reserve_tokens = normalize_compaction_budget_tokens(
        reserve_tokens,
        compaction_context_window,
    )
    summary_input_budget_tokens = compute_compaction_input_budget(
        compaction_context_window,
        reserve_tokens=normalized_reserve_tokens,
    )
    if summary_input_budget_tokens <= 0:
        return summary_input_budget_tokens, "non_positive_summary_input_budget"
    return summary_input_budget_tokens, None


def _resolve_replay_threshold_tokens(
    *,
    compaction_config: CompactionConfig,
    replay_window_tokens: int,
) -> int:
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    return resolve_effective_compaction_threshold(compaction_config, replay_window_tokens)


def _resolve_replay_budget_tokens(
    *,
    compaction_config: CompactionConfig,
    has_authored_compaction_config: bool,
    replay_window_tokens: int,
    threshold_tokens: int,
    static_prompt_tokens: int,
) -> int:
    ceiling_tokens = threshold_tokens
    if has_authored_compaction_config:
        normalized_reserve_tokens = normalize_compaction_budget_tokens(
            compaction_config.reserve_tokens,
            replay_window_tokens,
        )
        ceiling_tokens = min(ceiling_tokens, max(0, replay_window_tokens - normalized_reserve_tokens))
    return max(0, ceiling_tokens - static_prompt_tokens)
