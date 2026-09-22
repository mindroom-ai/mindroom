"""Select a judgment backend and record comparable outcome metrics for any task."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.judgment import LLMJudgmentConfig, TypeSafeJudgmentConfig
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.judgment.llm import judge_choice_with_llm, judge_with_llm
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.judgment import JudgmentConfig
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
    """Bind backend settings and credentials without starting an inference request."""
    return _create_evaluator(
        settings,
        runtime_paths,
        question_id=question_id,
        llm=lambda request, llm_settings: judge_with_llm(request, llm_settings, config, runtime_paths, owner=owner),
        typesafe=lambda client, request: client.judge(request, owner=owner, allow_network=True),
    )


def create_choice_evaluator(
    settings: JudgmentConfig,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    question_id: str,
) -> _JudgmentEvaluator[ChoiceDecision] | None:
    """Bind a comparative choice with the same credentials and metrics as boolean judgments."""
    return _create_evaluator(
        settings,
        runtime_paths,
        question_id=question_id,
        llm=lambda request, llm_settings: judge_choice_with_llm(
            request,
            llm_settings,
            config,
            runtime_paths,
            owner=owner,
        ),
        typesafe=lambda client, request: client.judge_choice(request, owner=owner, allow_network=True),
    )


def _create_evaluator[T](
    settings: JudgmentConfig,
    runtime_paths: RuntimePaths,
    *,
    question_id: str,
    llm: Callable[[JudgmentRequest, LLMJudgmentConfig], Awaitable[JudgmentResult[T]]],
    typesafe: Callable[[SystemOneClient, JudgmentRequest], Awaitable[JudgmentResult[T]]],
) -> _JudgmentEvaluator[T] | None:
    client: SystemOneClient | None = None
    if isinstance(settings, TypeSafeJudgmentConfig):
        key = (runtime_paths.env_value("TYPESAFE_API_KEY") or "").strip()
        if not key:
            logger.info(
                "Judgment fallback",
                question=question_id,
                backend=settings.provider,
                failure="missing_credential",
            )
            return None
        client = SystemOneClient(
            api_key=key,
            model=PINNED_MODEL,
            threshold=settings.threshold,
            timeout_seconds=settings.timeout_seconds,
        )

    async def evaluate(request: JudgmentRequest) -> JudgmentResult[T]:
        if isinstance(settings, LLMJudgmentConfig):
            result = await llm(request, settings)
        else:
            assert client is not None
            result = await typesafe(client, request)
        logger.info(
            "Judgment evaluated",
            question=question_id,
            backend=settings.provider,
            model=result.model_id,
            model_alias=settings.model if isinstance(settings, LLMJudgmentConfig) else None,
            decision=result.decision,
            probability=result.probability,
            threshold=settings.threshold if isinstance(settings, TypeSafeJudgmentConfig) else None,
            failure=result.failure,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            state_bytes=result.state_bytes,
            incomplete_reason=request.incomplete_reason,
        )
        return result

    return evaluate
