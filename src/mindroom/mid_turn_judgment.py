"""Bind room-level queued-message decisions to the shared judgment backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.mid_turn import MID_TURN_QUESTION, MidTurnGate, message_text_for_judgment

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope


def create_mid_turn_gate(
    config: Config,
    runtime_paths: RuntimePaths,
    envelope: MessageEnvelope,
    *,
    prompt: str,
    has_media: bool,
) -> MidTurnGate | None:
    """Bind one response's decision owner without performing inference."""
    settings = config.get_room_mid_turn(envelope.room_id, runtime_paths)
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
    return MidTurnGate(
        active_text=None if has_media or message_text_for_judgment(envelope) is None else prompt,
        evaluate=evaluate,
        instructions=settings.instructions,
    )
