"""Bind the shared participation rubric to a configured judgment backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, build_judgment_request
from mindroom.participation import PARTICIPATION_QUESTION, ParticipationDecider, ParticipationDecision

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.participation import ParticipationConfig
    from mindroom.constants import RuntimePaths


def create_participation_decider(
    participation: ParticipationConfig,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
) -> ParticipationDecider | None:
    """Map the common yes/no/abstain outcome to the participation lifecycle."""
    settings = participation.judgment
    if settings is None:
        return None
    evaluate = create_judgment_evaluator(
        settings,
        config,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:{agent_name}",
        question_id=PARTICIPATION_QUESTION.id,
    )
    if evaluate is None:
        return None
    instructions = participation.instructions

    async def decide(messages: tuple[JudgmentMessage, ...]) -> ParticipationDecision | None:
        request = build_judgment_request(PARTICIPATION_QUESTION, messages, instructions=instructions)
        result = await evaluate(request)
        if result.decision is None:
            return None
        return ParticipationDecision(
            action="respond" if result.decision else "stay_silent",
            reason=f"judgment_backend={settings.provider};decision={result.decision}",
        )

    return decide
