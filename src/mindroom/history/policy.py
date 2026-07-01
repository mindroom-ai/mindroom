"""Shared history budgeting and compaction-trigger policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.history.types import CompactionAvailabilityReason, CompactionDecision, ResolvedHistoryExecutionPlan
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
    compaction_runtime = _resolve_compaction_runtime_settings(
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
    hard_replay_budget_tokens = None
    if replay_window_tokens is not None and static_prompt_tokens is not None:
        hard_replay_budget_tokens = _resolve_replay_budget_without_compaction(
            compaction_config=compaction_config,
            replay_window_tokens=replay_window_tokens,
            static_prompt_tokens=static_prompt_tokens,
        )
        if compaction_config.enabled:
            threshold_tokens = _resolve_effective_compaction_threshold(compaction_config, replay_window_tokens)
            replay_budget_tokens = _resolve_replay_budget_tokens(
                compaction_config=compaction_config,
                has_authored_compaction_config=has_authored_compaction_config,
                replay_window_tokens=replay_window_tokens,
                threshold_tokens=threshold_tokens,
                static_prompt_tokens=static_prompt_tokens,
            )
        else:
            replay_budget_tokens = hard_replay_budget_tokens

    return ResolvedHistoryExecutionPlan(
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
        hard_replay_budget_tokens=hard_replay_budget_tokens,
    )


def classify_compaction_decision(  # noqa: PLR0911
    *,
    plan: ResolvedHistoryExecutionPlan,
    force_compact_before_next_run: bool,
    current_history_tokens: int | None,
) -> CompactionDecision:
    """Classify compaction as none or required before the next reply."""
    resolved_trigger_budget = plan.replay_budget_tokens
    resolved_hard_budget = plan.hard_replay_budget_tokens

    if force_compact_before_next_run:
        if plan.destructive_compaction_available:
            return CompactionDecision(
                mode="required",
                reason="forced",
                current_history_tokens=current_history_tokens,
                trigger_budget_tokens=resolved_trigger_budget,
                hard_budget_tokens=resolved_hard_budget,
                fitted_replay_tokens=(
                    0 if current_history_tokens is None else min(current_history_tokens, resolved_hard_budget or 0)
                ),
            )
        return CompactionDecision(
            mode="none",
            reason="forced_unavailable",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
        )

    if not plan.authored_compaction_enabled:
        return CompactionDecision(
            mode="none",
            reason="auto_disabled",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
        )
    if not plan.destructive_compaction_available:
        return CompactionDecision(
            mode="none",
            reason="compaction_unavailable",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
        )
    if current_history_tokens is None or resolved_trigger_budget is None:
        return CompactionDecision(
            mode="none",
            reason="missing_budget",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
        )
    if current_history_tokens <= resolved_trigger_budget:
        return CompactionDecision(
            mode="none",
            reason="under_trigger",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
            fitted_replay_tokens=current_history_tokens,
        )
    if resolved_hard_budget is not None and current_history_tokens > resolved_hard_budget:
        return CompactionDecision(
            mode="required",
            reason="history_exceeds_hard_budget",
            current_history_tokens=current_history_tokens,
            trigger_budget_tokens=resolved_trigger_budget,
            hard_budget_tokens=resolved_hard_budget,
            fitted_replay_tokens=resolved_hard_budget,
        )
    return CompactionDecision(
        mode="none",
        reason="within_hard_budget",
        current_history_tokens=current_history_tokens,
        trigger_budget_tokens=resolved_trigger_budget,
        hard_budget_tokens=resolved_hard_budget,
        fitted_replay_tokens=current_history_tokens,
    )


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
) -> tuple[int | None, CompactionAvailabilityReason | None]:
    if compaction_context_window is None:
        return None, "no_context_window"

    normalized_reserve_tokens = _normalize_compaction_budget_tokens(
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


def context_budget_after_reserve(context_window_tokens: int, reserve_tokens: int, spent_tokens: int = 0) -> int:
    """Return the usable context budget after clamped reserve and known prompt cost."""
    normalized_reserve_tokens = _normalize_compaction_budget_tokens(reserve_tokens, context_window_tokens)
    return max(0, context_window_tokens - normalized_reserve_tokens - spent_tokens)


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
        ceiling_tokens = min(
            ceiling_tokens,
            context_budget_after_reserve(replay_window_tokens, compaction_config.reserve_tokens),
        )
    return max(0, ceiling_tokens - static_prompt_tokens)


def _resolve_replay_budget_without_compaction(
    *,
    compaction_config: CompactionConfig,
    replay_window_tokens: int,
    static_prompt_tokens: int,
) -> int:
    return context_budget_after_reserve(replay_window_tokens, compaction_config.reserve_tokens, static_prompt_tokens)


@dataclass(frozen=True)
class _ResolvedCompactionRuntime:
    """Resolved model/window inputs needed for one compaction attempt."""

    model_name: str
    context_window: int | None


def _resolve_effective_compaction_threshold(compaction_config: CompactionConfig, context_window: int) -> int:
    """Resolve the soft replay trigger budget in tokens."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(context_window * threshold_percent)
    return int(context_window * 0.8)


def _normalize_compaction_budget_tokens(tokens: int, context_window: int | None) -> int:
    """Clamp one compaction knob against half of the available model window."""
    if context_window is None or context_window <= 0:
        return tokens
    return min(tokens, context_window // 2)


def _resolve_compaction_runtime_settings(
    *,
    config: Config,
    compaction_config: CompactionConfig,
    active_model_name: str,
    active_context_window: int | None,
) -> _ResolvedCompactionRuntime:
    """Resolve the effective compaction model name and usable window for one run."""
    model_name = compaction_config.model or active_model_name
    model_context_window = config.get_model_context_window(model_name)
    if compaction_config.model is not None:
        return _ResolvedCompactionRuntime(
            model_name=model_name,
            context_window=model_context_window,
        )
    return _ResolvedCompactionRuntime(
        model_name=model_name,
        context_window=model_context_window or active_context_window,
    )
