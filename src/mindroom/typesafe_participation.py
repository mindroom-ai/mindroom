"""Opt-in TypeSafe participation with explicit fallback to the existing decider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.client import SystemOneClient
from mindroom.judgment.state import PINNED_MODEL, JudgmentMessage, build_participation_judgment_request
from mindroom.logging_config import get_logger
from mindroom.participation import ParticipationDecider, ParticipationDecision

if TYPE_CHECKING:
    from mindroom.config.participation import RoomParticipationConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def create_participation_decider(
    room: RoomParticipationConfig,
    runtime_paths: RuntimePaths,
) -> ParticipationDecider | None:
    """Bind explicit room opt-in and instance credentials to one turn's lazy check."""
    settings = room.typesafe
    if settings is None:
        return None
    key = (runtime_paths.env_value("TYPESAFE_API_KEY") or "").strip()
    if not key:
        logger.info("TypeSafe participation fallback", failure="missing_credential")
        return None
    client = SystemOneClient(api_key=key, model=PINNED_MODEL, timeout_seconds=settings.timeout_seconds)
    owner = f"{runtime_paths.storage_root}:{room.agent}"
    instructions = room.instructions

    async def decide(messages: tuple[JudgmentMessage, ...]) -> ParticipationDecision | None:
        request = build_participation_judgment_request(messages, instructions=instructions)
        result = await client.judge(request, owner=owner, allow_network=True)
        probability = result.answer.probability if result.answer is not None else None
        logger.info(
            "TypeSafe participation judgment",
            model=result.model_id,
            probability=probability,
            threshold=settings.threshold,
            failure=result.failure,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            state_bytes=result.state_bytes,
            incomplete_reason=request.incomplete_reason,
        )
        if probability is None:
            return None
        return ParticipationDecision(
            action="respond" if probability >= settings.threshold else "stay_silent",
            reason=f"typesafe_probability={probability:.6f};threshold={settings.threshold:.6f}",
        )

    return decide
