"""Bounded decisions about continuing work while human messages are queued."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeGuard

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.state import JudgmentRequest

logger = get_logger(__name__)

# A long earlier message or reply snapshot reaches the judge as its start and end, so one big reply cannot disable judgment.
_CLIPPED_TEXT_CHARS = 2_000
# The redactor refuses input over 64 KiB, so longer text is scanned in overlapping chunks below that bound.
_REDACTION_CHUNK_CHARS = 60_000
_REDACTION_OVERLAP_CHARS = 4_000
_MAX_CONTEXT_MESSAGES = 63

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


def _clip(text: str) -> str:
    if len(text) <= _CLIPPED_TEXT_CHARS:
        return text
    half = _CLIPPED_TEXT_CHARS // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def _is_unredacted(text: str) -> bool:
    """Scan the whole text, so a credential that clipping cuts in half still blocks the request."""
    return all(
        redact_sensitive_text(chunk) == chunk
        for chunk in (
            text[start : start + _REDACTION_CHUNK_CHARS + _REDACTION_OVERLAP_CHARS]
            for start in range(0, len(text) or 1, _REDACTION_CHUNK_CHARS)
        )
    )


def _is_plain_text(text: str | None) -> TypeGuard[str]:
    return (
        text is not None
        and bool(text.strip())
        and "[attachments:" not in text
        and "Attachments sent with the current message" not in text
    )


def _is_whole_text(text: str | None) -> TypeGuard[str]:
    return _is_plain_text(text) and len(text) <= MAX_REQUEST_BYTES and _is_unredacted(text)


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
    evaluate: Callable[[JudgmentRequest], Awaitable[bool | None]]
    instructions: str = ""
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
        """Keep the latest Matrix-acknowledged text for subsequent queue snapshots."""
        self.visible_response_text = text

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
            finish = False
            request = self._request(pending)
            if isinstance(request, str):
                reason = request
            else:
                reason = request.incomplete_reason
                if request.complete:
                    try:
                        finish = await self.evaluate(request) is True
                    except Exception:
                        # Keep the existing behavior; SDK exceptions can contain private inputs.
                        reason = "judge_error"
            if reason is not None:
                logger.info("Mid-turn judgment skipped", reason=reason, queued_messages=len(pending))
            self._checked, self._finish = pending, finish
            return finish

    def _request(self, pending: tuple[QueuedMessage, ...]) -> JudgmentRequest | str:  # noqa: PLR0911
        """Build the judge's request, or name why the queue cannot be judged."""
        if self.conversation_context is None:
            return "history_unavailable"
        if not pending or len(pending) > 8:
            return "queued_message_count"
        # The judge compares the active request with each queued message, so both are sent whole.
        if not _is_whole_text(self.active_text):
            return "active_request_unjudgeable"
        queued = []
        for message in pending:
            if not _is_whole_text(message.text):
                return "queued_message_unjudgeable"
            if message.visible_response is None or not _is_unredacted(message.visible_response):
                return "visible_response_unavailable"
            queued.append({"text": message.text, "visible_response": _clip(message.visible_response)})
        history = self.conversation_context[-_MAX_CONTEXT_MESSAGES:]
        if not all(_is_plain_text(message.text) and _is_unredacted(message.text) for message in history):
            return "history_unjudgeable"
        evidence = json.dumps({"active_request": self.active_text, "queued_messages": queued}, ensure_ascii=False)
        context = [JudgmentMessage(message.sender, _clip(message.text)) for message in history]
        while True:
            request = build_judgment_request(
                MID_TURN_QUESTION,
                (*context, JudgmentMessage("user", evidence)),
                max_context_messages=_MAX_CONTEXT_MESSAGES + 1,
                instructions=self.instructions,
            )
            if request.incomplete_reason != "essential_input_too_large" or not context:
                return request
            # The oldest conversation gives way first.
            del context[0]
