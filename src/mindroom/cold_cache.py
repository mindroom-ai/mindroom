"""Move cold-cache turns to a smaller model when a judge accepts the request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, fit_judgment_request
from mindroom.logging_config import get_logger
from mindroom.providers.model_loading import CLAUDE_CACHE_PROVIDERS, canonical_provider

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
# The judge needs the gist of the conversation, not the whole thread.
_MAX_CONTEXT_MESSAGES = 7

_COLD_CACHE_QUESTION = JudgmentQuestion(
    id="cold_cache_model",
    instructions=(
        "Can a smaller, faster model answer the current request as well as the agent's main model? "
        "Use the recent conversation only to understand the current request."
    ),
    when_true=(
        "Short conversational replies, acknowledgements, simple factual answers, small edits, "
        "lookups, or routine tool use with clear steps."
    ),
    when_false=(
        "Multi-step reasoning, planning, coding or debugging, long or nuanced writing, ambiguous requests, "
        "high-stakes advice, or answers that depend on details from much earlier in the conversation."
    ),
)


def _prompt_cache_ttl_seconds(model_config: ModelConfig) -> int:
    """Return how long an idle thread's prompt cache lives, erring long when the provider is not exact."""
    claude = canonical_provider(model_config.provider) in CLAUDE_CACHE_PROVIDERS
    if claude and (model_config.extra_kwargs or {}).get("extended_cache_time") is False:
        return _DEFAULT_CACHE_TTL_SECONDS
    # Claude defaults to the 1h TTL, and 1h bounds OpenAI's in-memory retention.
    return _EXTENDED_CACHE_TTL_SECONDS


def _is_thread_cache_warm(session: AgentSession | None, model_config: ModelConfig, *, now: float) -> bool:
    """Whether the agent model served this thread's newest run within its cache TTL."""
    if session is None or not session.runs:
        return False
    newest = max(session.runs, key=lambda run: run.created_at)
    if newest.model != model_config.id:
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
    session: AgentSession | None,
    context: tuple[JudgmentMessage, ...] | None,
    prompt: str,
    now: float,
) -> str:
    """Return the smaller model only when the agent model's cache is cold and the judge accepts."""
    model_config = config.models.get(agent_model)
    if model_config is None or context is None or _is_thread_cache_warm(session, model_config, now=now):
        return agent_model
    request = fit_judgment_request(
        _COLD_CACHE_QUESTION,
        context,
        JudgmentMessage("user", prompt),
        instructions="",
        max_context_messages=_MAX_CONTEXT_MESSAGES,
    )
    if request is None or not request.complete:
        return agent_model
    evaluate = create_judgment_evaluator(
        settings.effective_judgment(),
        config,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:{agent_name}",
        question_id=_COLD_CACHE_QUESTION.id,
    )
    if evaluate is None:
        return agent_model
    result = await evaluate(request)
    chosen = settings.model if result.failure is None and result.decision is True else agent_model
    logger.info(
        "Cold cache model selected",
        agent=agent_name,
        agent_model=agent_model,
        chosen_model=chosen,
        cache_age_seconds=None if session is None or session.updated_at is None else now - session.updated_at,
        decision=result.decision,
    )
    return chosen
