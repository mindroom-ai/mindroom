"""Shared history budgeting and compaction-trigger policy."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.types import (
    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
    CompactionAvailabilityReason,
    CompactionDecision,
    ResolvedHistoryExecutionPlan,
)
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
    compaction_model_name = compaction_config.model or active_model_name
    compaction_context_window = config.get_model_context_window(compaction_model_name)
    if compaction_config.model is None and compaction_context_window is None:
        # Only the active model falls back to the runtime window; an explicit summary model has just its own.
        compaction_context_window = active_context_window
    replay_window_tokens = resolve_replay_window(
        active_context_window=active_context_window,
        configured_replay_window=compaction_config.replay_window_tokens,
    )
    summary_input_budget_tokens, unavailable_reason = _resolve_summary_input_budget(
        compaction_context_window=compaction_context_window,
        reserve_tokens=compaction_config.reserve_tokens,
    )
    fallback_summary_input_budget_tokens = _resolve_fallback_summary_input_budget(
        config=config,
        compaction_config=compaction_config,
    )

    threshold_tokens = None
    replay_budget_tokens = None
    hard_replay_budget_tokens = None
    if replay_window_tokens is not None and static_prompt_tokens is not None:
        hard_replay_budget_tokens = context_budget_after_reserve(
            replay_window_tokens,
            compaction_config.reserve_tokens,
            static_prompt_tokens,
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
        text_compaction_available=unavailable_reason is None,
        explicit_compaction_model=compaction_config.model is not None,
        compaction_model_name=compaction_model_name,
        replay_window_tokens=replay_window_tokens,
        trigger_threshold_tokens=threshold_tokens,
        reserve_tokens=compaction_config.reserve_tokens,
        static_prompt_tokens=static_prompt_tokens,
        replay_budget_tokens=replay_budget_tokens,
        summary_input_budget_tokens=summary_input_budget_tokens,
        compaction_timeout_seconds=compaction_config.timeout_seconds,
        unavailable_reason=unavailable_reason,
        hard_replay_budget_tokens=hard_replay_budget_tokens,
        compaction_fallback_model_name=compaction_config.fallback_model,
        compaction_fallback_summary_input_budget_tokens=fallback_summary_input_budget_tokens,
    )


def classify_compaction_decision(  # noqa: PLR0911
    *,
    plan: ResolvedHistoryExecutionPlan,
    force_compact_before_next_run: bool,
    current_history_tokens: int | None,
    allow_native_compaction: bool = True,
    summary_tokens: int = 0,
) -> CompactionDecision:
    """Classify compaction as none or required before the next reply.

    Between the trigger and hard budgets, native provider compaction owns the
    history, so a run without an active native route (a text-only provider, or a
    scheduled run with a history limit) compacts at the trigger budget instead,
    unless its saved summary already fills the trigger budget.
    """
    trigger_budget = plan.replay_budget_tokens
    hard_budget = plan.hard_replay_budget_tokens
    decision = partial(
        CompactionDecision,
        current_history_tokens=current_history_tokens,
        trigger_budget_tokens=trigger_budget,
        hard_budget_tokens=hard_budget,
    )

    if force_compact_before_next_run:
        if not plan.text_compaction_available:
            return decision(mode="none", reason="forced_unavailable")
        fitted_tokens = 0 if current_history_tokens is None else min(current_history_tokens, hard_budget or 0)
        return decision(mode="required", reason="forced", fitted_replay_tokens=fitted_tokens)
    if not plan.authored_compaction_enabled:
        return decision(mode="none", reason="auto_disabled")
    if not plan.text_compaction_available:
        return decision(mode="none", reason="compaction_unavailable")
    if current_history_tokens is None or trigger_budget is None:
        return decision(mode="none", reason="missing_budget")
    if current_history_tokens <= trigger_budget:
        return decision(mode="none", reason="under_trigger", fitted_replay_tokens=current_history_tokens)
    if hard_budget is not None and current_history_tokens > hard_budget:
        return decision(mode="required", reason="history_exceeds_hard_budget", fitted_replay_tokens=hard_budget)
    if not allow_native_compaction:
        # Compaction leaves the summary, so a trigger budget the summary already fills (or a zero budget, when the
        # static prompt reaches the threshold) cannot be met and would rewrite the summary every reply.
        if summary_tokens >= trigger_budget:
            return decision(mode="none", reason="trigger_unreachable", fitted_replay_tokens=current_history_tokens)
        return decision(mode="required", reason="over_trigger_native_disabled", fitted_replay_tokens=trigger_budget)
    return decision(mode="none", reason="within_hard_budget", fitted_replay_tokens=current_history_tokens)


def describe_compaction_unavailability(plan: ResolvedHistoryExecutionPlan) -> str | None:
    """Return a short description for one unavailable text-compaction reason."""
    reason = plan.unavailable_reason
    if reason == "no_context_window":
        if plan.explicit_compaction_model:
            return "no context_window is configured on the selected compaction model"
        return "no context_window is configured on the active model"
    if reason == "non_positive_summary_input_budget":
        return "the active compaction model leaves no usable summary input budget after reserve and prompt overhead"
    if reason == "summary_input_budget_without_retry_headroom":
        return (
            "the summary input budget must exceed "
            f"{2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS:,} tokens to provide meaningful headroom "
            "for a smaller retry"
        )
    return None


def _resolve_summary_input_budget(
    *,
    compaction_context_window: int | None,
    reserve_tokens: int,
) -> tuple[int | None, CompactionAvailabilityReason | None]:
    """Resolve a summary budget large enough for the request envelope and one degradation retry.

    Summary input uses the selected compaction model's real context window.
    The smaller replay window controls persisted replay and trigger planning only.
    Plans require more than twice the shared retry floor so halving leaves a
    genuinely smaller target with room for the request envelope and run content.
    """
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
    if summary_input_budget_tokens <= 2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS:
        return summary_input_budget_tokens, "summary_input_budget_without_retry_headroom"
    return summary_input_budget_tokens, None


def _resolve_fallback_summary_input_budget(
    *,
    config: Config,
    compaction_config: CompactionConfig,
) -> int | None:
    """Return a usable summary budget for the optional fallback model."""
    fallback_model_name = compaction_config.fallback_model
    if fallback_model_name is None:
        return None
    fallback_budget, unavailable_reason = _resolve_summary_input_budget(
        compaction_context_window=config.get_model_context_window(fallback_model_name),
        reserve_tokens=compaction_config.reserve_tokens,
    )
    return fallback_budget if unavailable_reason is None else None


def context_budget_after_reserve(context_window_tokens: int, reserve_tokens: int, spent_tokens: int = 0) -> int:
    """Return the usable context budget after clamped reserve and known prompt cost."""
    normalized_reserve_tokens = _normalize_compaction_budget_tokens(reserve_tokens, context_window_tokens)
    return max(0, context_window_tokens - normalized_reserve_tokens - spent_tokens)


def resolve_replay_window(
    *,
    active_context_window: int | None,
    configured_replay_window: int | None,
) -> int | None:
    """Cap persisted replay without changing the provider's real context window."""
    if configured_replay_window is None:
        return active_context_window
    if active_context_window is None:
        return configured_replay_window
    return min(active_context_window, configured_replay_window)


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


def _resolve_effective_compaction_threshold(compaction_config: CompactionConfig, replay_window_tokens: int) -> int:
    """Resolve the soft replay trigger budget in tokens."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(replay_window_tokens * threshold_percent)
    return int(replay_window_tokens * 0.8)


def _normalize_compaction_budget_tokens(tokens: int, context_window: int | None) -> int:
    """Clamp one compaction knob against half of the available model window."""
    if context_window is None or context_window <= 0:
        return tokens
    return min(tokens, context_window // 2)
