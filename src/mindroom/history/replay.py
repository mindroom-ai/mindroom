"""Materialize, estimate, and fit persisted history within one replay budget."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.history.message_content import (
    HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS,
    image_content_for_token_estimation,
    media_payload_snapshot,
    message_media_entries,
    render_message_content,
)
from mindroom.history.request_messages import scope_history_messages
from mindroom.history.run_visibility import replays_in_scope
from mindroom.history.summary_message import compaction_summary_fields
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings, ResolvedReplayPlan
from mindroom.llm.messages import Message
from mindroom.logging_config import get_logger
from mindroom.native_compaction import (
    checkpoint_estimated_tokens,
    checkpoint_items,
    native_checkpoint_id,
    native_replay_messages,
)
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import PortableReplayEstimator
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
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    native_route: str | None = None,
    replay_model: PortableReplayEstimator | None = None,
) -> int:
    """Estimate the durable summary plus visible persisted history for one run.

    The history is the selection a request under ``history_settings`` sends, so the estimate counts exactly what the
    model will read. Stale Anthropic replay fields are left in place because the char estimate never counts them.
    """
    policy = history_settings.policy
    history_messages = scope_history_messages(
        session.runs,
        scope=scope,
        num_history_runs=policy.num_history_runs,
        num_history_messages=policy.num_history_messages,
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
    )
    tokens, has_checkpoint = _route_tokens(history_messages, replay_model=replay_model, native_route=native_route)
    # A checkpoint covers the summary message before it, which native projection drops.
    return tokens if has_checkpoint else estimate_summary_tokens(session) + tokens


def _route_tokens(
    messages: list[Message],
    *,
    replay_model: PortableReplayEstimator | None,
    native_route: str | None,
) -> tuple[int, bool]:
    """Estimate messages as a route replays them, and report whether a native checkpoint replaced their prefix."""
    checkpoint_tokens = 0
    if native_route is not None:
        projected = native_replay_messages(messages, native_route)
        messages = []
        for message in projected:
            if items := checkpoint_items(message, native_route):
                checkpoint_tokens += checkpoint_estimated_tokens(items)
            else:
                messages.append(message)
    tokens = _estimate_messages_tokens(messages, replay_model=replay_model, native_route=native_route)
    return checkpoint_tokens + tokens, checkpoint_tokens > 0


def estimate_request_messages_tokens(
    messages: Sequence[Message],
    *,
    replay_model: PortableReplayEstimator | None,
    native_route: str | None = None,
) -> int:
    """Estimate one provider request's messages the way replay planning estimates history."""
    return _route_tokens(list(messages), replay_model=replay_model, native_route=native_route)[0]


def _estimate_messages_tokens(
    history_messages: list[Message],
    *,
    replay_model: PortableReplayEstimator | None,
    native_route: str | None,
) -> int:
    uses_visual_tokens = replay_model is not None and replay_model.portable_replay_uses_visual_tokens()
    attachment_tokens = _attachment_fallback_tokens(history_messages)
    history_messages = [_without_attachments(message) for message in history_messages]
    estimation_messages = (
        history_messages
        if uses_visual_tokens
        else [_without_image_transport(message, strip_content_blocks=False) for message in history_messages]
    )
    provider_estimate = (
        replay_model.estimate_portable_replay_tokens(estimation_messages)
        if replay_model is not None and (estimation_messages or native_route is None)
        else None
    )
    provider_accounts_for_images = uses_visual_tokens and provider_estimate is not None
    canonical_messages = [
        _without_image_transport(message, strip_content_blocks=provider_accounts_for_images)
        for message in history_messages
    ]
    # TODO(simplify): Canonical text counts as total characters // 4 without a native route but as a per-message
    # ceiling with one, so one history gets two estimates. Use one rounding rule. Operators would see compaction and
    # replay trimming trigger a few tokens earlier or later.
    canonical_tokens = (
        _estimate_history_messages_tokens(canonical_messages)
        if native_route is None
        else sum((_estimated_message_chars(message) + 3) // 4 for message in canonical_messages)
    )
    image_fallback_tokens = 0 if provider_accounts_for_images else _image_fallback_tokens(history_messages)
    return max(canonical_tokens, provider_estimate or 0) + image_fallback_tokens + attachment_tokens


def _without_attachments(message: Message) -> Message:
    """Drop attached files, audio, and video for estimation; formatting them would count encoded bytes as text."""
    updates = {
        field: None
        for field, value in (("files", message.files), ("audio", message.audio), ("videos", message.videos))
        if value
    }
    return replace(message, **updates) if updates else message


def _attachment_fallback_tokens(messages: list[Message]) -> int:
    """Charge each attached file, audio, or video a bounded allowance instead of its encoded size."""
    count = sum(len(message.files or []) + len(message.audio or []) + len(message.videos or []) for message in messages)
    return count * HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS


def _without_image_transport(message: Message, *, strip_content_blocks: bool) -> Message:
    """Project image fields for estimation without changing saved messages."""
    updates: dict[str, object] = {}
    if strip_content_blocks and isinstance(message.content, list):
        updates["content"] = [image_content_for_token_estimation(block) for block in message.content]
    if message.images:
        updates["images"] = None
    return replace(message, **updates) if updates else message


def compaction_summary_message(summary: str, *, run_local: bool) -> Message:
    """Render one compaction summary as the user message that replaces the history it covers."""
    return Message(**compaction_summary_fields(summary, run_local=run_local))


def estimate_summary_tokens(session: ConversationSession) -> int:
    """Estimate the tokens the scope's summary message adds to a request."""
    summary = current_summary_text(session)
    if summary is None:
        return 0
    return estimate_text_tokens(str(compaction_summary_message(summary, run_local=False).content))


def _estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    return sum(_estimated_message_chars(message) for message in messages) // 4


def scope_visible_runs(session: ConversationSession, scope: HistoryScope) -> list[AnyRunRecord]:
    """Return this scope's model-history-visible runs in stored order."""
    return [run for run in session.runs if replays_in_scope(run, scope)]


def current_summary_text(session: ConversationSession) -> str | None:
    """Return a nonempty durable summary, normalized for replay."""
    if session.summary is None:
        return None
    return session.summary.text.strip() or None


def compaction_marker(session: ConversationSession, scope: HistoryScope) -> tuple[str | None, str | None]:
    """Identify what the scope's replayed history was last compacted into: its summary and newest native checkpoint.

    A changed marker means a compaction rewrote the history that requests replay.
    """
    checkpoint = next(
        (
            checkpoint_id
            for run in reversed(scope_visible_runs(session, scope))
            for message in reversed(run.messages)
            if (checkpoint_id := native_checkpoint_id(message)) is not None
        ),
        None,
    )
    return current_summary_text(session), checkpoint


def _estimated_message_chars(message: Message) -> int:
    content_chars = len(render_message_content(message))
    tool_call_chars = len(stable_serialize(message.tool_calls)) if message.tool_calls else 0
    return content_chars + tool_call_chars + _estimate_message_media_chars(message)


def _estimate_message_media_chars(message: Message) -> int:
    """Estimate serialized character cost for a message's media payloads."""
    # TODO(simplify): Estimation strips every non-empty media field before counting characters, so this adds only two
    # characters per empty media list. Drop the media count. Operators would see estimates move by a few tokens at most.
    return sum(
        len(stable_serialize(media_payload_snapshot(media_value)))
        for _tag, media_value in message_media_entries(message)
        if media_value is not None
    )


def _image_fallback_tokens(messages: list[Message]) -> int:
    """Charge bounded visual input without serializing image transport as text."""
    return sum(len(message.images or []) for message in messages) * HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS


def plan_replay_that_fits(
    *,
    session: ConversationSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    current_history_tokens: int,
    replay_model: PortableReplayEstimator | None = None,
) -> ResolvedReplayPlan:
    """Return the safest persisted-replay plan that fits the current run budget."""
    summary_tokens = estimate_summary_tokens(session)
    if summary_tokens > available_history_budget:
        raise HistorySummaryBudgetError(summary_tokens=summary_tokens, available_tokens=available_history_budget)
    if current_history_tokens <= available_history_budget:
        return configured_replay_plan(
            history_settings=history_settings,
            estimated_tokens=current_history_tokens,
        )

    limit_mode, max_limit = _context_window_guard_limit_bounds(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    fitting_limit, fitting_tokens = _find_fitting_history_limit_for_budget(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        limit_mode=limit_mode,
        max_limit=max_limit,
        replay_model=replay_model,
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
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    policy = history_settings.policy
    if policy.mode == "messages":
        return "messages", policy.limit or 0
    visible_run_count = len(scope_visible_runs(session, scope))
    return "runs", visible_run_count if policy.mode == "all" else min(policy.limit or 0, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: ConversationSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
    replay_model: PortableReplayEstimator | None = None,
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
            scope=scope,
            history_settings=replace(history_settings, policy=HistoryPolicy(mode=limit_mode, limit=mid)),
            replay_model=replay_model,
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


def has_effective_persisted_replay(
    *,
    session: ConversationSession,
    scope: HistoryScope,
    replay_plan: ResolvedReplayPlan,
) -> bool:
    """Report whether a summary or permitted raw runs will reach the model."""
    if current_summary_text(session) is not None:
        return True
    if not replay_plan.add_history_to_context or replay_plan.num_history_runs == 0:
        return False
    return bool(scope_visible_runs(session, scope))
