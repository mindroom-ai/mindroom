"""Bounded decisions about continuing work while human messages are queued."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.answers import JudgmentResult
    from mindroom.judgment.state import JudgmentRequest

MID_TURN_QUESTION = JudgmentQuestion(
    id="finish_current_turn",
    instructions="May the active task finish before the queued human messages are handled in a later turn?",
    when_true=(
        "All queued messages are clearly independent of the active task, simple acknowledgements, "
        "or explicitly ask to continue unchanged. Finishing the original task would still respect the user's intent."
    ),
    when_false=(
        "Any queued message corrects, cancels, redirects, adds a relevant constraint, or changes the active task. "
        "Also false when context is incomplete, the relationship is unclear, or continued work could conflict "
        "with the newer instructions. Visible progress is what Matrix had acknowledged when each message "
        "was queued, not a complete execution log or proof that operations are free of side effects. "
        "The completed tool batch cannot be undone; wrap_up requests a handoff before further tool use."
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
    visible_response_text: str | None = ""
    _checked: tuple[QueuedMessage, ...] | None = None
    _finish: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def record_visible_response(self, text: str) -> None:
        """Keep only bounded Matrix-acknowledged text for subsequent queue snapshots."""
        self.visible_response_text = text if len(text) <= MAX_REQUEST_BYTES else None

    async def should_finish(self, pending: tuple[QueuedMessage, ...]) -> bool:
        """Only a valid affirmative judgment can suppress the existing wrap-up notice."""
        async with self._lock:
            if self._checked == pending:
                return self._finish
            texts = (self.active_text, *(message.text for message in pending))
            if (
                not pending
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
                (JudgmentMessage("user", evidence),),
                instructions=self.instructions,
            )
            finish = False
            if request.complete:
                try:
                    result = await self.evaluate(request)
                    finish = result.failure is None and result.decision is True
                except Exception:
                    # Keep the existing behavior; SDK exceptions can contain private inputs.
                    finish = False
            self._checked, self._finish = pending, finish
            return finish
