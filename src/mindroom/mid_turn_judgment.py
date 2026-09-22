"""Bind agent-level queued-message decisions to the shared judgment backend."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.config.judgment import TypeSafeJudgmentConfig
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage
from mindroom.logging_config import get_logger
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.mid_turn import MID_TURN_QUESTION, MidTurnGate, message_text_for_judgment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


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
    threshold = settings.judgment.threshold if isinstance(settings.judgment, TypeSafeJudgmentConfig) else None

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


def conversation_context_for_mid_turn(
    history: Sequence[ResolvedVisibleMessage],
    *,
    source_event_ids: tuple[str, ...],
    thread_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[JudgmentMessage, ...] | None:
    """Keep complete public text before this turn's first source, never future queued input.

    Preserve the conversation rather than guessing which old request is still active.
    Missing, partial, media, or oversized history cannot authorize continued tool use.
    """
    if is_thread_history_degraded(history) or (
        isinstance(history, ThreadHistoryResult) and not history.is_full_history
    ):
        return None
    if not history:
        # Only a proven new thread establishes that no earlier task context exists.
        return () if thread_id in source_event_ids else None
    internal_senders = current_internal_sender_ids(config, runtime_paths)
    context: list[JudgmentMessage] = []
    size = 0
    for message in history:
        if message.event_id in source_event_ids:
            return tuple(context)
        if message.content.get("msgtype", "m.text") not in {"m.text", "m.notice"} or message.content.get(
            ATTACHMENT_IDS_KEY,
        ):
            return None
        size += len(message.body)
        if not message.body.strip() or size > MAX_REQUEST_BYTES:
            return None
        role = (
            "assistant"
            if message.sender in internal_senders and not message.content.get(ORIGINAL_SENDER_KEY)
            else "user"
        )
        context.append(JudgmentMessage(role, message.body))
    return None
