"""Materialize, estimate, and fit persisted history within one replay budget."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.history.request_messages import request_layout, scope_history_messages
from mindroom.history.run_visibility import replays_in_scope
from mindroom.history.summary_message import compaction_summary_message
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
    owner_scope,
    replayed_summary_text,
)
from mindroom.llm.provider import ModelRequest
from mindroom.logging_config import get_logger
from mindroom.native_compaction import native_checkpoint_id
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import ConversationSession


logger = get_logger(__name__)


class HistorySummaryBudgetError(RuntimeError):
    """The saved summary cannot fit in the current run's history budget."""

    def __init__(self, *, summary_tokens: int, available_tokens: int) -> None:
        super().__init__(
            "Saved conversation summary exceeds the available history budget "
            f"({summary_tokens} estimated tokens; {available_tokens} available). "
            "Choose a model with a larger context window or reduce the current prompt.",
        )


def estimate_prompt_visible_history_tokens(
    *,
    session: ConversationSession,
    history_settings: ResolvedHistorySettings,
    model: ModelProvider,
) -> int:
    """Estimate the durable summary plus visible persisted history of the scope owning ``session`` for one run.

    The history is the selection a request under ``history_settings`` sends, estimated as ``model``'s route renders
    it.
    """
    history = prompt_visible_history(session, history_settings)
    return model.request_tokens(ModelRequest(messages=request_layout(summary=session.summary, history=history)))


def prompt_visible_history(session: ConversationSession, history_settings: ResolvedHistorySettings) -> list[Message]:
    """Return the stored history a request under ``history_settings`` replays for the scope owning ``session``."""
    policy = history_settings.policy
    return scope_history_messages(
        session.runs,
        scope=owner_scope(session),
        num_history_runs=policy.num_history_runs,
        num_history_messages=policy.num_history_messages,
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
    )


def estimate_summary_tokens(session: ConversationSession, model: ModelProvider) -> int:
    """Estimate the tokens the scope's summary message adds to a request ``model`` receives."""
    summary = current_summary_text(session)
    if summary is None:
        return 0
    return estimate_summary_text_tokens(summary, model)


def estimate_summary_text_tokens(summary: str, model: ModelProvider, *, upper_bound: bool = False) -> int:
    """Estimate the tokens the summary message holding ``summary`` adds to a request ``model`` receives.

    The route's size of that message, never below its text's count. ``upper_bound`` keeps the count from undercounting
    while the vocabulary loads, as a check before storing a summary, which every later reply replays, needs.
    """
    message = compaction_summary_message(summary, run_local=False)
    tokens = count(model.token_counter, str(message.content), upper_bound=upper_bound)
    return max(tokens, model.request_tokens(ModelRequest(messages=(message,))))


def scope_visible_runs(session: ConversationSession) -> list[AnyRunRecord]:
    """Return the model-history-visible runs of the scope owning ``session``, in stored order."""
    scope = owner_scope(session)
    return [run for run in session.runs if replays_in_scope(run, scope)]


def current_summary_text(session: ConversationSession) -> str | None:
    """Return the session's durable summary as replay reads it (``replayed_summary_text``), or None without one."""
    return replayed_summary_text(session.summary.text) if session.summary is not None else None


def compaction_marker(session: ConversationSession) -> tuple[str | None, str | None]:
    """Identify what ``session``'s replayed history was last compacted into: its summary and newest native checkpoint.

    A changed marker means a compaction rewrote the history that requests replay.
    """
    checkpoint = next(
        (
            checkpoint_id
            for run in reversed(scope_visible_runs(session))
            for message in reversed(run.messages)
            if (checkpoint_id := native_checkpoint_id(message)) is not None
        ),
        None,
    )
    return current_summary_text(session), checkpoint


def plan_replay_that_fits(
    *,
    session: ConversationSession,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    current_history_tokens: int,
    model: ModelProvider,
) -> ResolvedReplayPlan:
    """Return the safest plan for replaying the persisted history of the scope owning ``session`` within the budget.

    ``current_history_tokens`` holds the summary, so a history that fits replays whole even when the summary's estimate,
    padded by its margin, would not fit.
    """
    if current_history_tokens <= available_history_budget:
        return configured_replay_plan(
            history_settings=history_settings,
            estimated_tokens=current_history_tokens,
        )
    summary_tokens = estimate_summary_tokens(session, model)
    if summary_tokens > available_history_budget:
        raise HistorySummaryBudgetError(summary_tokens=summary_tokens, available_tokens=available_history_budget)

    limit_mode, max_limit = _context_window_guard_limit_bounds(session=session, history_settings=history_settings)
    fitting_limit, fitting_tokens = _find_fitting_history_limit_for_budget(
        session=session,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        limit_mode=limit_mode,
        max_limit=max_limit,
        model=model,
    )
    if fitting_limit > 0:
        policy = HistoryPolicy(mode=limit_mode, limit=fitting_limit)
        return ResolvedReplayPlan(
            mode="limited",
            estimated_tokens=fitting_tokens,
            add_history_to_context=True,
            num_history_runs=policy.num_history_runs,
            num_history_messages=policy.num_history_messages,
        )

    # Zero raw runs still replays history, which is what places the summary before the prompt.
    return ResolvedReplayPlan(
        mode="disabled",
        estimated_tokens=summary_tokens,
        add_history_to_context=True,
        num_history_runs=0,
    )


def _context_window_guard_limit_bounds(
    *,
    session: ConversationSession,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    policy = history_settings.policy
    if policy.mode == "messages":
        return "messages", policy.limit or 0
    visible_run_count = len(scope_visible_runs(session))
    return "runs", visible_run_count if policy.mode == "all" else min(policy.limit or 0, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: ConversationSession,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
    model: ModelProvider,
) -> tuple[int, int]:
    if max_limit <= 0 or available_history_budget <= 0:
        return 0, 0

    low = 1
    high = max_limit
    best = 0
    best_tokens = 0
    while low <= high:
        mid = (low + high) // 2
        candidate_tokens = estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=replace(history_settings, policy=HistoryPolicy(mode=limit_mode, limit=mid)),
            model=model,
        )
        if candidate_tokens <= available_history_budget:
            best = mid
            best_tokens = candidate_tokens
            low = mid + 1
        else:
            high = mid - 1
    return best, best_tokens


def log_replay_plan(
    *,
    replay_plan: ResolvedReplayPlan,
    scope: HistoryScope,
    available_history_budget: int,
    current_history_tokens: int,
) -> None:
    """Explain a budget-driven reduction of persisted replay."""
    if replay_plan.mode == "configured":
        return

    if replay_plan.mode == "limited":
        logger.warning(
            "Replay planner reduced persisted replay for this run",
            scope=scope.key,
            num_history_runs=replay_plan.num_history_runs,
            num_history_messages=replay_plan.num_history_messages,
            estimated_tokens=current_history_tokens,
            fitted_tokens=replay_plan.estimated_tokens,
            available_history_budget=available_history_budget,
        )
        return

    logger.warning(
        "Replay planner disabled raw persisted replay for this run",
        scope=scope.key,
        estimated_tokens=current_history_tokens,
        fitted_tokens=replay_plan.estimated_tokens,
        available_history_budget=available_history_budget,
    )


def configured_replay_plan(
    *,
    history_settings: ResolvedHistorySettings,
    estimated_tokens: int,
) -> ResolvedReplayPlan:
    """Preserve authored replay limits when the history already fits."""
    return ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimated_tokens,
        add_history_to_context=True,
        num_history_runs=history_settings.policy.num_history_runs,
        num_history_messages=history_settings.policy.num_history_messages,
    )


def has_effective_persisted_replay(*, session: ConversationSession, replay_plan: ResolvedReplayPlan) -> bool:
    """Report whether a summary or permitted raw runs of the scope owning ``session`` will reach the model."""
    if current_summary_text(session) is not None:
        return True
    if not replay_plan.add_history_to_context or replay_plan.num_history_runs == 0:
        return False
    return bool(scope_visible_runs(session))
