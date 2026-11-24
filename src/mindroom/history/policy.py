"""Shared history budgeting and compaction-trigger policy."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.types import (
    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
    CompactionAvailabilityReason,
    CompactionDecision,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig


def _compaction_input_budget(
    context_window: int,
    *,
    reserve_tokens: int,
    prompt_overhead_tokens: int = 2000,
    safety_margin_ratio: float = 0.10,
) -> int:
    """Compute the max input tokens available for a compaction summary request.

    Subtracts output reserve, prompt overhead (system prompt + response format),
    and a safety margin from the compaction model's context window.
    """
    safety = int(context_window * safety_margin_ratio)
    budget = context_window - reserve_tokens - prompt_overhead_tokens - safety
    return max(0, budget)


def resolve_history_execution_plan(
    *,
    config: Config,
    history_settings: ResolvedHistorySettings,
    active_model_name: str,
    active_context_window: int | None,
) -> ResolvedHistoryExecutionPlan:
    """Resolve all history-budget policy for one run scope in one place.

    Its budgets bound a whole request, the system prompt, tools, history, and turn together, as providers report one.
    """
    compaction_config = history_settings.compaction_config
    compaction_model_name = compaction_config.model or active_model_name
    compaction_context_window = config.get_model_context_window(compaction_model_name)
    if compaction_config.model is None and compaction_context_window is None:
        # Only the active model falls back to the runtime window; an explicit summary model has just its own.
        compaction_context_window = active_context_window
    replay_window_tokens = _resolve_replay_window(
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
    trigger_tokens = None
    limit_tokens = None
    if replay_window_tokens is not None:
        limit_tokens = context_budget_after_reserve(replay_window_tokens, compaction_config.reserve_tokens)
        # Without automatic compaction nothing compacts at the trigger, so a request fits the limit.
        trigger_tokens = limit_tokens
        if history_settings.automatic_compaction:
            threshold_tokens = _resolve_effective_compaction_threshold(compaction_config, replay_window_tokens)
            trigger_tokens = min(threshold_tokens, limit_tokens)

    return ResolvedHistoryExecutionPlan(
        automatic_compaction=history_settings.automatic_compaction,
        text_compaction_available=unavailable_reason is None,
        explicit_compaction_model=compaction_config.model is not None,
        compaction_model_name=compaction_model_name,
        replay_window_tokens=replay_window_tokens,
        trigger_threshold_tokens=threshold_tokens,
        reserve_tokens=compaction_config.reserve_tokens,
        trigger_tokens=trigger_tokens,
        limit_tokens=limit_tokens,
        summary_input_budget_tokens=summary_input_budget_tokens,
        compaction_timeout_seconds=compaction_config.timeout_seconds,
        unavailable_reason=unavailable_reason,
        compaction_fallback_model_name=compaction_config.fallback_model,
        compaction_fallback_summary_input_budget_tokens=fallback_summary_input_budget_tokens,
    )


def classify_compaction_decision(  # noqa: PLR0911
    *,
    plan: ResolvedHistoryExecutionPlan,
    force_compact_before_next_run: bool,
    request_tokens: int,
    summary_request_tokens: int,
    allow_native_compaction: bool,
) -> CompactionDecision:
    """Classify compaction as none or required before the next reply, from its first request's size.

    Between the trigger and the limit, native provider compaction owns the history, so a run without an active native
    route (a text-only provider, or a scheduled run with a history limit) compacts at the trigger instead, unless the
    request would still reach the trigger with the summary as its only history (``summary_request_tokens``).
    """
    trigger = plan.trigger_tokens
    limit = plan.limit_tokens
    decision = partial(
        CompactionDecision,
        request_tokens=request_tokens,
        trigger_tokens=trigger,
        limit_tokens=limit,
    )

    if force_compact_before_next_run:
        if not plan.text_compaction_available:
            return decision(mode="none", reason="forced_unavailable")
        return decision(mode="required", reason="forced")
    if not plan.automatic_compaction:
        return decision(mode="none", reason="auto_disabled")
    if not plan.text_compaction_available:
        return decision(mode="none", reason="compaction_unavailable")
    if trigger is None:
        return decision(mode="none", reason="missing_budget")
    if request_tokens <= trigger:
        return decision(mode="none", reason="under_trigger")
    if limit is not None and request_tokens > limit:
        return decision(mode="required", reason="request_exceeds_limit")
    if not allow_native_compaction:
        # Compaction leaves the summary, so a trigger the request reaches with the summary alone (or one the static
        # prompt reaches) cannot be met and would rewrite the summary every reply.
        if summary_request_tokens >= trigger:
            return decision(mode="none", reason="trigger_unreachable")
        return decision(mode="required", reason="over_trigger_native_disabled")
    return decision(mode="none", reason="within_limit")


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
    summary_input_budget_tokens = _compaction_input_budget(
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


def context_budget_after_reserve(context_window_tokens: int, reserve_tokens: int) -> int:
    """Return the usable context budget after the clamped reserve."""
    normalized_reserve_tokens = _normalize_compaction_budget_tokens(reserve_tokens, context_window_tokens)
    return max(0, context_window_tokens - normalized_reserve_tokens)


def _resolve_replay_window(
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


def _resolve_effective_compaction_threshold(compaction_config: CompactionConfig, replay_window_tokens: int) -> int:
    """Resolve the soft replay trigger budget in tokens."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(replay_window_tokens * threshold_percent)
    return int(replay_window_tokens * 0.8)


def _normalize_compaction_budget_tokens(tokens: int, context_window: int) -> int:
    """Clamp one compaction knob against half of the available model window."""
    if context_window <= 0:
        return tokens
    return min(tokens, context_window // 2)
