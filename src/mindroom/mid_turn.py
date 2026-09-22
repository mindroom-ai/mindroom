"""Bounded decisions about continuing work while human messages are queued."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.answers import JudgmentResult
    from mindroom.judgment.state import JudgmentRequest

logger = get_logger(__name__)

MID_TURN_QUESTION = JudgmentQuestion(
    id="interrupt_current_turn",
    instructions="Does any newly queued message require an immediate change to, pause of, or stop of the active task?",
    when_true=(
        "A new message requests stopping or pausing the active task, corrects it, changes a relevant "
        "requirement, or explicitly asks to switch tasks immediately. A relevant change takes priority "
        "over praise or 'keep going' in the same message or queue."
    ),
    when_false=(
        "The messages acknowledge progress, express thanks, ask to continue unchanged, or request "
        "NOT interrupting. 'Do not interrupt' means continue; 'do not continue' means stop. "
        "Unrelated questions or tasks can wait until afterward, even without explicit 'later' wording. "
        "Mere relevance to the task does not make praise or thanks an interruption."
    ),
)


def message_text_for_judgment(envelope: MessageEnvelope) -> str | None:
    """Only complete ordinary text is eligible for the external queued-message judge."""
    if envelope.source_kind != MESSAGE_SOURCE_KIND or envelope.attachment_ids:
        return None
    return envelope.body


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """Immutable pending input; missing text requires the existing wrap-up behavior."""

    event_id: str
    text: str | None
    visible_response: str | None = ""


@dataclass
class MidTurnGate:
    """Reuse one decision per pending snapshot within one active response."""

    active_text: str | None
    evaluate: Callable[[JudgmentRequest], Awaitable[JudgmentResult]]
    instructions: str = ""
    continuation_threshold: float = 0.8
    conversation_context: tuple[JudgmentMessage, ...] | None = ()
    visible_response_text: str | None = ""
    on_defer: Callable[[str], Awaitable[None]] | None = None
    _acknowledged: set[str] = field(default_factory=set)
    _checked: tuple[QueuedMessage, ...] | None = None
    _finish: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def bind_conversation_context(self, context: tuple[JudgmentMessage, ...] | None) -> None:
        """Freeze public history after lock acquisition, invalidating any earlier decision."""
        self.conversation_context = context
        self._checked = None
        self._finish = False

    def record_visible_response(self, text: str) -> None:
        """Keep only bounded Matrix-acknowledged text for subsequent queue snapshots."""
        self.visible_response_text = text if len(text) <= MAX_REQUEST_BYTES else None

    async def acknowledge_deferred(self, message: QueuedMessage) -> None:
        """Attempt one acknowledgement per message after the caller accepts the decision."""
        if self.on_defer is None or message.event_id in self._acknowledged:
            return
        self._acknowledged.add(message.event_id)
        await self.on_defer(message.event_id)

    async def should_finish(self, pending: tuple[QueuedMessage, ...]) -> bool:
        """Continue only with sufficient confidence that no interruption is needed."""
        async with self._lock:
            if self._checked == pending:
                return self._finish
            texts = (
                self.active_text,
                *(message.text for message in pending),
                *(message.text for message in self.conversation_context or ()),
            )
            if (
                self.conversation_context is None
                or not pending
                or len(pending) > 8
                or any(
                    text is None
                    or not text.strip()
                    or len(text) > MAX_REQUEST_BYTES
                    or "[attachments:" in text
                    or "Attachments sent with the current message" in text
                    or redact_sensitive_text(text) != text
                    for text in texts
                )
            ):
                self._checked, self._finish = pending, False
                return False
            if any(
                message.visible_response is None
                or len(message.visible_response) > MAX_REQUEST_BYTES
                or redact_sensitive_text(message.visible_response) != message.visible_response
                for message in pending
            ):
                self._checked, self._finish = pending, False
                return False
            evidence = json.dumps(
                {
                    "active_request": self.active_text,
                    "queued_messages": [
                        {"text": message.text, "visible_response": message.visible_response} for message in pending
                    ],
                },
                ensure_ascii=False,
            )
            request = build_judgment_request(
                MID_TURN_QUESTION,
                (*(self.conversation_context or ()), JudgmentMessage("user", evidence)),
                max_context_messages=64,
                instructions=self.instructions,
            )
            finish = False
            if request.complete:
                try:
                    result = await self.evaluate(request)
                    finish = (
                        result.failure is None
                        and result.decision is not None
                        and (
                            1 - result.probability >= self.continuation_threshold
                            if result.probability is not None
                            else result.decision is False
                        )
                    )
                    logger.info(
                        "Mid-turn continuation evaluated",
                        finish_current_turn=finish,
                        continuation_probability=1 - result.probability if result.probability is not None else None,
                        continuation_threshold=self.continuation_threshold if result.probability is not None else None,
                        failure=result.failure,
                    )
                except Exception:
                    # Keep the existing behavior; SDK exceptions can contain private inputs.
                    finish = False
            self._checked, self._finish = pending, finish
            return finish
