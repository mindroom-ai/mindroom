"""Materialize, estimate, and fit persisted history within one replay budget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.utils.message import filter_tool_calls

from mindroom.constants import prompt_roles_for_history_storage
from mindroom.history.message_content import (
    HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS,
    image_content_for_token_estimation,
    media_payload_snapshot,
    message_media_entries,
    project_history_media_for_replay,
    render_message_content,
)
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings, ResolvedReplayPlan
from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.logging_config import get_logger
from mindroom.native_compaction import checkpoint_estimated_tokens, checkpoint_items, native_replay_messages
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.agent import Agent
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from agno.team import Team

    from mindroom.native_compaction import NativeCompactionModel


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
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    native_route: str | None = None,
    replay_model: NativeCompactionModel | None = None,
) -> int:
    """Estimate the durable summary plus visible persisted history for one run."""
    summary_tokens = _estimate_session_summary_tokens(current_summary_text(session))
    history_messages = _history_messages_for_estimation(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    checkpoint_tokens = 0
    if native_route is not None:
        projected = native_replay_messages(history_messages, native_route)
        history_messages = []
        for message in projected:
            if items := checkpoint_items(message, native_route):
                checkpoint_tokens += checkpoint_estimated_tokens(items)
            else:
                history_messages.append(message)
    return (
        summary_tokens
        + checkpoint_tokens
        + _estimate_messages_tokens(history_messages, replay_model=replay_model, native_route=native_route)
    )


def estimate_request_messages_tokens(
    messages: Sequence[Message],
    *,
    replay_model: NativeCompactionModel | None,
) -> int:
    """Estimate one provider request's messages the way replay planning estimates history."""
    return _estimate_messages_tokens(list(messages), replay_model=replay_model, native_route=None)


def _estimate_messages_tokens(
    history_messages: list[Message],
    *,
    replay_model: NativeCompactionModel | None,
    native_route: str | None,
) -> int:
    uses_visual_tokens = replay_model is not None and replay_model.portable_replay_uses_visual_tokens()
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
    canonical_tokens = (
        _estimate_history_messages_tokens(canonical_messages)
        if native_route is None
        else sum((_estimated_message_chars(message) + 3) // 4 for message in canonical_messages)
    )
    image_fallback_tokens = 0 if provider_accounts_for_images else _image_fallback_tokens(history_messages)
    return max(canonical_tokens, provider_estimate or 0) + image_fallback_tokens


def _without_image_transport(message: Message, *, strip_content_blocks: bool) -> Message:
    """Project image fields for estimation without changing saved messages."""
    updates: dict[str, object] = {}
    if strip_content_blocks and isinstance(message.content, list):
        updates["content"] = [image_content_for_token_estimation(block) for block in message.content]
    if message.images:
        updates["images"] = None
    return message.model_copy(update=updates) if updates else message


_COMPACTION_SUMMARY_MARKER = "mindroom_compaction_summary"


def compaction_summary_message(summary: str, *, from_history: bool) -> Message:
    """Render one compaction summary as the user message that replaces the history it covers."""
    return Message(
        role="user",
        content=(
            "The earlier part of this conversation was compacted into this summary:\n\n"
            f"<compacted_history>\n{summary}\n</compacted_history>\n\n"
            "The conversation continues after this summary. If it records progress on the request that follows, "
            "continue that work from its next steps instead of starting over."
        ),
        provider_data={_COMPACTION_SUMMARY_MARKER: True},
        from_history=from_history,
    )


def compaction_summary_text(message: Message) -> str:
    """Return the summary a rendered compaction summary message carries."""
    content = str(message.content)
    start = content.index("<compacted_history>\n") + len("<compacted_history>\n")
    return content[start : content.rindex("\n</compacted_history>")]


def is_compaction_summary(message: Message) -> bool:
    """Return whether one message is a rendered compaction summary."""
    return isinstance(message.provider_data, dict) and message.provider_data.get(_COMPACTION_SUMMARY_MARKER) is True


def _estimate_session_summary_tokens(summary_text: str | None) -> int:
    """Estimate prompt-visible tokens contributed by one stored session summary."""
    if summary_text is None:
        return 0
    normalized_summary = summary_text.strip()
    if not normalized_summary:
        return 0
    return estimate_text_tokens(str(compaction_summary_message(normalized_summary, from_history=True).content))


def _estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    if not messages:
        return 0
    return sum(_estimated_message_chars(message) for message in messages) // 4


def _history_messages_for_estimation(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return the prompt-visible history messages for token estimation only.

    No deepcopy: filter_tool_calls copies any message it modifies and only the
    list itself is mutated. Stale Anthropic replay fields are left in place
    because the char estimate never counts them.
    """
    history_messages = list(
        _session_history_messages(
            session=session,
            scope=scope,
            history_settings=history_settings,
        ),
    )
    if history_settings.max_tool_calls_from_history is not None and history_messages:
        filter_tool_calls(history_messages, history_settings.max_tool_calls_from_history)
    return project_history_media_for_replay(history_messages)


def _session_history_messages(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    limit = history_settings.policy.limit
    if scope.kind == "team":
        return _team_session_history_messages(
            session=cast("TeamSession", session),
            scope_id=scope.scope_id,
            history_settings=history_settings,
            limit=limit,
        )
    return _agent_session_history_messages(
        session=cast("AgentSession", session),
        scope_id=scope.scope_id,
        history_settings=history_settings,
        limit=limit,
    )


def _agent_session_history_messages(
    *,
    session: AgentSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(agent_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(agent_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(agent_id=scope_id, skip_roles=skip_roles)


def _team_session_history_messages(
    *,
    session: TeamSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(team_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(team_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(team_id=scope_id, skip_roles=skip_roles)


def history_skip_roles(history_settings: ResolvedHistorySettings) -> list[str]:
    """Return prompt roles that should never be materialized as persisted history."""
    return sorted(prompt_roles_for_history_storage(history_settings.system_message_role))


def scope_visible_runs(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    """Return this scope's model-history-visible runs in stored order."""
    return _runs_for_scope([run for run in session.runs or [] if is_model_history_visible_run(run)], scope)


def _runs_for_scope(
    runs: Sequence[RunOutput | TeamRunOutput],
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    """Filter model-history-visible runs down to one persisted history scope."""
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def current_summary_text(session: AgentSession | TeamSession) -> str | None:
    """Return a nonempty durable summary, normalized for replay."""
    if session.summary is None:
        return None
    return session.summary.summary.strip() or None


def _estimated_message_chars(message: Message) -> int:
    content_chars = len(render_message_content(message))
    tool_call_chars = len(stable_serialize(message.tool_calls)) if message.tool_calls else 0
    return content_chars + tool_call_chars + _estimate_message_media_chars(message)


def _estimate_message_media_chars(message: Message) -> int:
    """Estimate serialized character cost for a message's media payloads."""
    media_chars = 0
    for _tag, media_value in message_media_entries(message):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(media_payload_snapshot(media_value)))
    return media_chars


def _image_fallback_tokens(messages: list[Message]) -> int:
    """Charge bounded visual input without serializing image transport as text."""
    return sum(len(message.images or []) for message in messages) * HISTORY_VIEWED_IMAGE_FALLBACK_TOKENS


def plan_replay_that_fits(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    current_history_tokens: int,
    replay_model: NativeCompactionModel | None = None,
) -> ResolvedReplayPlan:
    """Return the safest persisted-replay plan that fits the current run budget."""
    summary_tokens = _session_summary_replay_tokens(session)
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
        num_history_runs, num_history_messages = _history_limit_fields(limit_mode, fitting_limit)
        return ResolvedReplayPlan(
            mode="limited",
            estimated_tokens=fitting_tokens,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
            num_history_messages=num_history_messages,
        )

    # Zero raw runs still replays history, which is what places the summary before the prompt.
    return ResolvedReplayPlan(
        mode="disabled",
        estimated_tokens=_session_summary_replay_tokens(session),
        add_history_to_context=True,
        num_history_runs=0,
    )


def apply_replay_plan(
    *,
    target: Agent | Team,
    replay_plan: ResolvedReplayPlan,
) -> None:
    """Apply one resolved persisted-replay plan to a live Agent or Team."""
    target.add_history_to_context = replay_plan.add_history_to_context
    target.num_history_runs = replay_plan.num_history_runs
    target.num_history_messages = replay_plan.num_history_messages


def _context_window_guard_limit_bounds(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    configured_limit = history_settings.policy.limit or 0
    if history_settings.policy.mode == "messages":
        return "messages", configured_limit

    visible_run_count = len(scope_visible_runs(session, scope))
    if history_settings.policy.mode == "all":
        return "runs", visible_run_count
    return "runs", min(configured_limit, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
    replay_model: NativeCompactionModel | None = None,
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
            history_settings=_history_settings_with_limit(
                history_settings,
                mode=limit_mode,
                limit=mid,
            ),
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
    num_history_runs, num_history_messages = _history_limit_fields(
        history_settings.policy.mode,
        history_settings.policy.limit,
    )
    return ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimated_tokens,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
    )


def _history_settings_with_limit(
    history_settings: ResolvedHistorySettings,
    *,
    mode: Literal["runs", "messages"],
    limit: int,
) -> ResolvedHistorySettings:
    return ResolvedHistorySettings(
        policy=HistoryPolicy(mode=mode, limit=limit),
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
        system_message_role=history_settings.system_message_role,
    )


def _history_limit_fields(
    mode: Literal["all", "runs", "messages"],
    limit: int | None,
) -> tuple[int | None, int | None]:
    if mode == "runs":
        return limit, None
    if mode == "messages":
        return None, limit
    return None, None


def has_effective_persisted_replay(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    replay_plan: ResolvedReplayPlan,
) -> bool:
    """Report whether a summary or permitted raw runs will reach the model."""
    if _session_has_summary_replay(session):
        return True
    if not replay_plan.add_history_to_context or replay_plan.num_history_runs == 0:
        return False
    return bool(scope_visible_runs(session, scope))


def _session_has_summary_replay(session: AgentSession | TeamSession) -> bool:
    if session.summary is None:
        return False
    return bool(session.summary.summary.strip())


def _session_summary_replay_tokens(session: AgentSession | TeamSession) -> int:
    if session.summary is None:
        return 0
    return _estimate_session_summary_tokens(session.summary.summary)
