"""Bind judgment backends and record comparable outcome metrics for any task."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.judgment.llm import judge_with_llm
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.judgment import JudgmentConfig, TypeSafeJudgmentConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.judgment.answers import ChoiceDecision, JudgmentResult
    from mindroom.judgment.state import JudgmentRequest

logger = get_logger(__name__)

type _JudgmentEvaluator[T] = Callable[[JudgmentRequest], Awaitable[JudgmentResult[T]]]


def create_judgment_evaluator(
    settings: JudgmentConfig,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    question_id: str,
) -> _JudgmentEvaluator[bool] | None:
    """Bind a boolean backend without starting an inference request."""
    if isinstance(settings, LLMJudgmentConfig):
        evaluate = partial(judge_with_llm, settings=settings, config=config, runtime_paths=runtime_paths, owner=owner)
    else:
        client = _typesafe_client(settings, runtime_paths, question_id=question_id)
        if client is None:
            return None
        evaluate = partial(client.judge, owner=owner, allow_network=True)
    return _logged_evaluator(evaluate, settings, question_id=question_id)


def create_choice_evaluator(
    settings: TypeSafeJudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    question_id: str,
) -> _JudgmentEvaluator[ChoiceDecision] | None:
    """Bind a System One choice using the shared credentials, limits and metrics."""
    client = _typesafe_client(settings, runtime_paths, question_id=question_id)
    if client is None:
        return None
    return _logged_evaluator(
        partial(client.judge_choice, owner=owner, allow_network=True),
        settings,
        question_id=question_id,
    )


def _typesafe_client(
    settings: TypeSafeJudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    question_id: str,
) -> SystemOneClient | None:
    key = (runtime_paths.env_value("TYPESAFE_API_KEY") or "").strip()
    if not key:
        logger.info("Judgment fallback", question=question_id, backend=settings.provider, failure="missing_credential")
        return None
    return SystemOneClient(
        api_key=key,
        model=PINNED_MODEL,
        threshold=settings.threshold,
        timeout_seconds=settings.timeout_seconds,
    )


def _logged_evaluator[T](
    evaluate: _JudgmentEvaluator[T],
    settings: JudgmentConfig,
    *,
    question_id: str,
) -> _JudgmentEvaluator[T]:
    async def logged(request: JudgmentRequest) -> JudgmentResult[T]:
        result = await evaluate(request)
        logger.info(
            "Judgment evaluated",
            question=question_id,
            backend=settings.provider,
            model=result.model_id,
            model_alias=settings.model if isinstance(settings, LLMJudgmentConfig) else None,
            decision=result.decision,
            probability=result.probability,
            threshold=settings.threshold if not isinstance(settings, LLMJudgmentConfig) else None,
            failure=result.failure,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            state_bytes=result.state_bytes,
            incomplete_reason=request.incomplete_reason,
        )
        return result

    return logged
