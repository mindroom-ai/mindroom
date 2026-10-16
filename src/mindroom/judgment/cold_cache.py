"""Move a thread whose prompt cache went cold to the agent's cheap model when a judge accepts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, fit_judgment_request
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.cold_cache import ColdCacheConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)

# The cheap model takes over the whole thread, so the judge sees as much of it as the request budget fits.
_MAX_CONTEXT_MESSAGES = 63

_COLD_CACHE_QUESTION_ID = "cold_cache_model"


def _cold_cache_question(agent_model_id: str, cheap_model_id: str) -> JudgmentQuestion:
    """Name both models, since a judge can only compare models it knows."""
    return JudgmentQuestion(
        id=_COLD_CACHE_QUESTION_ID,
        instructions=(
            f"Can the cheaper model {cheap_model_id} continue this conversation, including the current request, "
            f"as well as the agent's main model {agent_model_id}? "
            "The cheaper model would take over the whole conversation, "
            "so judge the conversation so far and the current request together."
        ),
        when_true=(
            "The whole conversation is simple: short conversational exchanges, acknowledgements, "
            "simple factual answers, small edits, lookups, or routine tool use with clear steps."
        ),
        when_false=(
            "The conversation so far or the current request involves multi-step reasoning, planning, "
            "coding or debugging, long or nuanced writing, ambiguity, or high-stakes advice, "
            "even when the current request alone looks simple."
        ),
    )


def idle_seconds_before_turn(
    history: Sequence[ResolvedVisibleMessage],
    *,
    agent_user_id: str,
    source_event_ids: tuple[str, ...],
    now: float,
) -> float | None:
    """Return how long the agent's last reply before this turn has been idle, or None when it never replied."""
    last_ms = None
    for message in history:
        if message.event_id in source_event_ids:
            break
        if message.sender == agent_user_id:
            # A streamed reply's final edit marks its last provider request.
            last_ms = max(message.timestamp, message.edited_timestamp or 0)
    return None if last_ms is None else now - last_ms / 1000


async def select_cold_cache_model(
    settings: ColdCacheConfig,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_model: str,
    cheap_model: str,
    idle_seconds: float | None,
    context: tuple[JudgmentMessage, ...] | None,
    prompt: str,
) -> str:
    """Return the cheap model only when the thread has been idle past the cache lifetime and the judge accepts."""
    decision, reason = await _judge(
        settings,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        agent_model=agent_model,
        cheap_model=cheap_model,
        idle_seconds=idle_seconds,
        context=context,
        prompt=prompt,
    )
    chosen = cheap_model if decision is True else agent_model
    logger.info(
        "Cold cache model selected",
        agent=agent_name,
        agent_model=agent_model,
        chosen_model=chosen,
        idle_seconds=idle_seconds,
        decision=decision,
        reason=reason,
    )
    return chosen


async def _judge(
    settings: ColdCacheConfig,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_model: str,
    cheap_model: str,
    idle_seconds: float | None,
    context: tuple[JudgmentMessage, ...] | None,
    prompt: str,
) -> tuple[bool | None, str]:
    """Return the judgment decision, or None, with why it was or was not made."""
    if idle_seconds is None:
        # A new thread has no cache yet, and its first request says little about the rest.
        return None, "no_earlier_reply"
    if idle_seconds < settings.idle_seconds:
        return None, "warm"
    if context is None:
        return None, "history_unavailable"
    request = fit_judgment_request(
        _cold_cache_question(config.models[agent_model].id, config.models[cheap_model].id),
        context,
        JudgmentMessage("user", prompt),
        instructions="",
        max_context_messages=_MAX_CONTEXT_MESSAGES,
    )
    if request is None or not request.complete:
        return None, "request_incomplete"
    evaluate = create_judgment_evaluator(
        settings.effective_judgment(cheap_model),
        config,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:{agent_name}",
        question_id=_COLD_CACHE_QUESTION_ID,
    )
    if evaluate is None:
        return None, "judgment_unavailable"
    result = await evaluate(request)
    return (result.decision if result.failure is None else None), "judged"
