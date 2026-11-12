"""Bind the shared participation rubric to a configured judgment backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, fit_judgment_request
from mindroom.participation import PARTICIPATION_QUESTION, ParticipationDecider, ParticipationDecision
from mindroom.requester_identity import mindroom_user_id

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
    # The judge knows the agent's name, so a message naming it reads as addressed to it.
    instructions = f"The assistant is {config.entity_display_name(agent_name)}. {participation.instructions}".strip()

    async def decide(messages: tuple[JudgmentMessage, ...]) -> ParticipationDecision | str:
        if not messages:
            return "missing_essential_input"
        agents = current_internal_sender_ids(config, runtime_paths) - {mindroom_user_id(config, runtime_paths)}
        request = fit_judgment_request(
            PARTICIPATION_QUESTION,
            messages[:-1],
            messages[-1],
            instructions=instructions,
            max_context_messages=len(messages) - 1,
            agent_ids=agents,
        )
        if request is None:
            return "essential_input_redacted"
        result = await evaluate(request)
        if result.decision is None:
            return request.incomplete_reason or result.failure or "abstained"
        return ParticipationDecision(
            action="respond" if result.decision else "stay_silent",
            reason=f"judgment_backend={settings.provider};decision={result.decision}",
        )

    return decide
