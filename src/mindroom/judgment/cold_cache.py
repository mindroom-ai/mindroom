"""Move cold-cache turns to the agent's cheap model when a judge accepts the request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, fit_judgment_request
from mindroom.logging_config import get_logger
from mindroom.providers.model_loading import CLAUDE_CACHE_PROVIDERS, canonical_provider, effective_model_id

if TYPE_CHECKING:
    from agno.session.agent import AgentSession

    from mindroom.config.cold_cache import ColdCacheConfig
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Anthropic's TTLs, refreshed on every cache hit.
_EXTENDED_CACHE_TTL_SECONDS = 3600
_DEFAULT_CACHE_TTL_SECONDS = 300
# OpenAI's extended retention, the default for GPT-5.5 and earlier outside Zero Data Retention.
_LONGEST_PROVIDER_CACHE_TTL_SECONDS = 24 * 3600
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


def _prompt_cache_ttl_seconds(model_config: ModelConfig) -> int:
    """Return how long an idle thread's prompt cache lives, erring long when the provider is not exact."""
    if canonical_provider(model_config.provider) not in CLAUDE_CACHE_PROVIDERS:
        # Other providers' retention is not known exactly, so only their longest lifetime counts as expired.
        return _LONGEST_PROVIDER_CACHE_TTL_SECONDS
    if (model_config.extra_kwargs or {}).get("extended_cache_time") is False:
        return _DEFAULT_CACHE_TTL_SECONDS
    # MindRoom enables the 1h TTL for Claude by default.
    return _EXTENDED_CACHE_TTL_SECONDS


def _is_thread_cache_warm(session: AgentSession | None, model_config: ModelConfig, *, now: float) -> bool:
    """Whether the agent model served this thread's newest run within its cache TTL."""
    if session is None or not session.runs:
        return False
    # Interrupted-turn replays carry no model, so the newest run that names one decides.
    served = max((run for run in session.runs if run.model), key=lambda run: run.created_at, default=None)
    if served is not None and served.model != effective_model_id(model_config):
        return False
    # The session is written at or after the last provider request, so this errs toward warm.
    return session.updated_at is None or now - session.updated_at < _prompt_cache_ttl_seconds(model_config)


async def select_cold_cache_model(
    settings: ColdCacheConfig,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_model: str,
    cheap_model: str,
    session: AgentSession | None,
    context: tuple[JudgmentMessage, ...] | None,
    prompt: str,
    now: float,
) -> str:
    """Return the cheap model only when the agent model's cache is cold and the judge accepts."""
    decision, reason = await _judge(
        settings,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        agent_model=agent_model,
        cheap_model=cheap_model,
        session=session,
        context=context,
        prompt=prompt,
        now=now,
    )
    chosen = cheap_model if decision is True else agent_model
    logger.info(
        "Cold cache model selected",
        agent=agent_name,
        agent_model=agent_model,
        chosen_model=chosen,
        cache_age_seconds=None if session is None or session.updated_at is None else now - session.updated_at,
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
    session: AgentSession | None,
    context: tuple[JudgmentMessage, ...] | None,
    prompt: str,
    now: float,
) -> tuple[bool | None, str]:
    """Return the judgment decision, or None, with why it was or was not made."""
    model_config = config.models.get(agent_model)
    cheap_config = config.models.get(cheap_model)
    if model_config is None or cheap_config is None or _is_thread_cache_warm(session, model_config, now=now):
        return None, "warm"
    if context is None:
        return None, "history_unavailable"
    request = fit_judgment_request(
        _cold_cache_question(model_config.id, cheap_config.id),
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
