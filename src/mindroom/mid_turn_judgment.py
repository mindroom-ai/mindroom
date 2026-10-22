"""Bind agent-level queued-message decisions to the shared judgment backend."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.logging_config import get_logger
from mindroom.mid_turn import MID_TURN_QUESTION, MidTurnGate, message_text_for_judgment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.state import JudgmentRequest


logger = get_logger(__name__)


def create_mid_turn_gate(
    config: Config,
    runtime_paths: RuntimePaths,
    envelope: MessageEnvelope,
    *,
    prompt: str,
    has_media: bool,
    on_defer: Callable[[str, str], Awaitable[None]] | None = None,
) -> MidTurnGate | None:
    """Bind one response's decision owner without performing inference."""
    agent = config.agents.get(envelope.agent_name)
    settings = agent.mid_turn if agent is not None else None
    if settings is None:
        return None
    evaluate = create_judgment_evaluator(
        settings.judgment,
        config,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:{envelope.agent_name}",
        question_id=MID_TURN_QUESTION.id,
    )
    if evaluate is None:
        return None
    threshold = None if isinstance(settings.judgment, LLMJudgmentConfig) else settings.judgment.threshold

    async def may_finish(request: JudgmentRequest) -> bool:
        result = await evaluate(request)
        probability = 1 - result.probability if result.probability is not None else None
        finish = (
            result.failure is None
            and result.decision is not None
            and (
                result.decision is False if threshold is None else probability is not None and probability >= threshold
            )
        )
        logger.info(
            "Mid-turn continuation evaluated",
            finish_current_turn=finish,
            continuation_probability=probability,
            continuation_threshold=threshold,
            failure=result.failure,
        )
        return finish

    return MidTurnGate(
        active_text=None if has_media or message_text_for_judgment(envelope) is None else prompt,
        evaluate=may_finish,
        instructions=settings.instructions,
        on_defer=(
            partial(on_defer, settings.defer_reaction)
            if on_defer is not None and settings.defer_reaction is not None
            else None
        ),
    )
