"""Bind judgment backends and record comparable outcome metrics for any task."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING

from mindroom.config.judgment import LLMJudgmentConfig, OpenAIDecisionsJudgmentConfig
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_provider, get_api_key_for_service
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.execution import SHARED_CAPACITY
from mindroom.judgment.llm import judge_with_llm
from mindroom.judgment.openai_decisions import OPENAI_DECISIONS
from mindroom.judgment.typesafe import SYSTEM_ONE
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.judgment import JudgmentConfig, ProbabilityJudgmentConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.judgment.answers import ChoiceDecision, JudgmentResult
    from mindroom.judgment.execution import JudgmentCapacity
    from mindroom.judgment.state import JudgmentRequest

logger = get_logger(__name__)

type _JudgmentEvaluator[T] = Callable[[JudgmentRequest], Awaitable[JudgmentResult[T]]]
# A batch has one question id per question, so its logs name the batch instead.
_BATCH = "batch"


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
        client = _probability_client(settings, runtime_paths, question_id=question_id)
        if client is None:
            return None
        evaluate = partial(client.judge, owner=owner, allow_network=True)
    return _logged_evaluator(evaluate, settings, question_id=question_id)


def create_choice_evaluator(
    settings: ProbabilityJudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    question_id: str,
) -> _JudgmentEvaluator[ChoiceDecision] | None:
    """Bind a probability backend's choice using the shared credentials, limits and metrics."""
    client = _probability_client(settings, runtime_paths, question_id=question_id)
    if client is None:
        return None
    return _logged_evaluator(
        partial(client.judge_choice, owner=owner, allow_network=True),
        settings,
        question_id=question_id,
    )


@dataclass(frozen=True, slots=True)
class BatchEvaluator:
    """A bound batch backend: ``fits`` says whether a request's wire body fits, ``evaluate`` judges it."""

    evaluate: _JudgmentEvaluator[tuple[float | None, ...]]
    fits: Callable[[JudgmentRequest], bool]


def create_batch_evaluator(
    settings: ProbabilityJudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    capacity: JudgmentCapacity,
) -> BatchEvaluator | None:
    """Bind a probability backend's batched predicates under their own capacity."""
    client = _probability_client(settings, runtime_paths, question_id=_BATCH, capacity=capacity)
    if client is None:
        return None
    return BatchEvaluator(
        evaluate=_logged_evaluator(
            partial(client.judge_many, owner=owner, allow_network=True),
            settings,
            question_id=_BATCH,
        ),
        fits=client.batch_fits,
    )


def _probability_client(
    settings: ProbabilityJudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    question_id: str,
    capacity: JudgmentCapacity = SHARED_CAPACITY,
) -> JudgmentClient | None:
    credentials_service = None
    if isinstance(settings, OpenAIDecisionsJudgmentConfig):
        credentials_service = settings.credentials_service
        wire = OPENAI_DECISIONS
        key = (
            get_api_key_for_provider("openai", runtime_paths)
            if credentials_service is None
            else get_api_key_for_service(credentials_service, runtime_paths)
        )
    else:
        wire, key = SYSTEM_ONE, get_secret_from_env("TYPESAFE_API_KEY", runtime_paths=runtime_paths)
    key = (key or "").strip()
    if not key:
        logger.info(
            "Judgment fallback",
            question=question_id,
            backend=settings.provider,
            failure="missing_credential",
            credentials_service=credentials_service,
        )
        return None
    return JudgmentClient(
        api_key=key,
        wire=wire,
        threshold=settings.threshold,
        timeout_seconds=settings.timeout_seconds,
        capacity=capacity,
    )


def _logged_decision(decision: object) -> object:
    """A batch's decision as counts, so one rerank logs two numbers instead of one probability per candidate."""
    if isinstance(decision, tuple):
        return {"probabilities": len(decision), "refused": sum(probability is None for probability in decision)}
    return decision


def _logged_evaluator[T](
    evaluate: _JudgmentEvaluator[T],
    settings: JudgmentConfig,
    *,
    question_id: str,
) -> _JudgmentEvaluator[T]:
    model_alias = settings.model if isinstance(settings, LLMJudgmentConfig) else None
    threshold = None if isinstance(settings, LLMJudgmentConfig) else settings.threshold

    def _emit(
        request: JudgmentRequest,
        *,
        result: JudgmentResult[T] | None,
        failure: str | None,
        latency_ms: int,
    ) -> None:
        logger.info(
            "Judgment evaluated",
            question=question_id,
            backend=settings.provider,
            model=None if result is None else result.model_id,
            model_alias=model_alias,
            decision=None if result is None else _logged_decision(result.decision),
            probability=None if result is None else result.probability,
            threshold=threshold,
            failure=failure,
            latency_ms=latency_ms,
            input_tokens=None if result is None else result.input_tokens,
            output_tokens=None if result is None else result.output_tokens,
            state_bytes=request.state_bytes if result is None else result.state_bytes,
            incomplete_reason=request.incomplete_reason,
        )

    async def logged(request: JudgmentRequest) -> JudgmentResult[T]:
        started = perf_counter()
        try:
            result = await evaluate(request)
        except asyncio.CancelledError:
            _emit(
                request,
                result=None,
                failure="cancelled",
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
            )
            raise
        _emit(request, result=result, failure=result.failure, latency_ms=result.latency_ms)
        return result

    return logged
