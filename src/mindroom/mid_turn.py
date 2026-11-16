"""Bounded decisions about continuing work while human messages are queued."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeGuard

from mindroom.attachments.storage import mentions_attachments
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.judgment.state import (
    MAX_REQUEST_BYTES,
    JudgmentMessage,
    JudgmentQuestion,
    clip_is_unredacted,
    clip_judgment_text,
    fit_judgment_request,
    is_unredacted,
)
from mindroom.logging_config import get_logger
from mindroom.tool_system.events import body_with_tool_tags

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

_MAX_CONTEXT_MESSAGES = 63

MID_TURN_QUESTION = JudgmentQuestion(
    id="interrupt_current_turn",
    instructions="Does any newly queued message require an immediate change to, pause of, or stop of the active task?",
    when_true=(
        "A new message requests stopping or pausing the active task, corrects it, changes a relevant "
        "requirement, or explicitly asks to switch tasks immediately, whether the active request's requester "
        "or another person sends it. A relevant change takes priority over praise or 'keep going' in the same "
        "message or queue."
    ),
    when_false=(
        "The messages acknowledge progress, express thanks, ask to continue unchanged, or request "
        "NOT interrupting. 'Do not interrupt' means continue; 'do not continue' means stop. "
        "Unrelated questions or tasks can wait until afterward, even without explicit 'later' wording. "
        "Mere relevance to the task does not make praise or thanks an interruption. A message from another "
        "person that is not about the active task is conversation among people and can wait."
    ),
)


def _is_plain_text(text: str | None) -> TypeGuard[str]:
    return text is not None and bool(text.strip()) and not mentions_attachments(text)


def _is_whole_text(text: str | None) -> TypeGuard[str]:
    return _is_plain_text(text) and len(text) <= MAX_REQUEST_BYTES and is_unredacted(text)


def message_text_for_judgment(envelope: MessageEnvelope) -> str | None:
    """Only complete ordinary text is eligible for the external queued-message judge."""
    if envelope.source_kind != MESSAGE_SOURCE_KIND or envelope.attachment_ids:
        return None
    return envelope.body


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """One message queued during an active reply; without text it cannot be judged."""

    event_id: str
    text: str | None
    visible_response: str | None = ""
    requester_id: str | None = None
    # Unknown counts as mentioning someone, so an unjudged queue holding it wraps the reply up.
    mentions_nobody: bool = False


@dataclass
class MidTurnGate:
    """Reuse one decision per pending snapshot within one active response."""

    active_text: str | None
    # True when the queue can wait, False when it cannot, None when the judge gave no answer.
    evaluate: Callable[[JudgmentRequest], Awaitable[bool | None]]
    # The person the active reply answers; None for a reply no person asked for, such as a scheduled one.
    requester_id: str | None = None
    instructions: str = ""
    conversation_context: tuple[JudgmentMessage, ...] | None = ()
    visible_response_text: str | None = ""
    on_defer: Callable[[str], Awaitable[None]] | None = None
    _acknowledged: set[str] = field(default_factory=set)
    _checked: tuple[QueuedMessage, ...] | None = None
    _finish: bool = False
    # Messages of a queue that could wait, and those of the checked queue the judge said the reply cannot wait for.
    _waited: set[str] = field(default_factory=set)
    _stopped_for: frozenset[str] = frozenset()
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def bind_conversation_context(self, context: tuple[JudgmentMessage, ...] | None) -> None:
        """Freeze public history after lock acquisition, invalidating any earlier decision."""
        self.conversation_context = context
        self._checked = None
        self._finish = False

    def record_visible_response(self, text: str, tool_trace: Sequence[ToolTraceEntry]) -> None:
        """Keep the latest Matrix-acknowledged text, with its tool calls as tags, for later queue snapshots."""
        # Without a trace, marker-shaped lines are the agent's own text.
        self.visible_response_text = body_with_tool_tags(text, tool_trace) if tool_trace else text

    async def _acknowledge_deferred(self, message: QueuedMessage) -> None:
        """Attempt one acknowledgement per message after the caller accepts the decision."""
        if self.on_defer is None or message.event_id in self._acknowledged:
            return
        self._acknowledged.add(message.event_id)
        await self.on_defer(message.event_id)

    def _between_people(self, message: QueuedMessage) -> bool:
        """Return whether another person than the requester wrote this message, mentioning nobody."""
        return self.requester_id is not None and message.mentions_nobody and message.requester_id != self.requester_id

    async def defers(
        self,
        pending: tuple[QueuedMessage, ...],
        *,
        current: Callable[[], bool],
        judge: bool = True,
    ) -> bool:
        """Return whether ``pending`` can wait for the active reply, acknowledging each message while ``current`` holds.

        Without ``judge``, ``pending`` cannot wait, whatever an earlier judgment of it said, and the judge is not asked.
        """
        try:
            finish = await self._should_finish(pending, judge=judge)
        except Exception:
            # Callers hand off as on a failed judgment, so a judgment that raises never costs the active reply.
            finish = False
        if not finish:
            return False
        for message in pending:
            # Matrix delivery can yield to a newer correction or a concurrent handoff.
            if not current():
                return False
            # Whether the agent answers another person's remark is decided after the reply, not promised here.
            if self._between_people(message):
                continue
            try:
                await self._acknowledge_deferred(message)
            except Exception:
                logger.warning("mid_turn_reaction_failed")
        return current()

    async def _should_finish(self, pending: tuple[QueuedMessage, ...], *, judge: bool) -> bool:
        """Continue only with sufficient confidence that no interruption is needed."""
        async with self._lock:
            if judge and self._checked == pending:
                return self._finish
            finish = stops = False
            # An earlier judgment weighed the work that ran then, not a reply that only waits on jobs.
            request = self._request(pending) if judge else "only_waiting_on_jobs"
            if isinstance(request, str):
                reason = request
            else:
                reason = request.incomplete_reason
                if request.complete:
                    try:
                        verdict = await self.evaluate(request)
                    except Exception:
                        # Logged without the exception, whose SDK message can contain private inputs.
                        reason = "judge_error"
                    else:
                        finish, stops = verdict is True, verdict is False
                        reason = "no_answer" if verdict is None else None
            if reason is not None:
                # Unjudged, the reply wraps up, unless only other people's messages to nobody in particular wait.
                finish = judge and all(self._between_people(message) for message in pending)
                logger.info(
                    "Mid-turn judgment skipped",
                    reason=reason,
                    queued_messages=len(pending),
                    outcome="waits" if finish else "wraps_up",
                )
            event_ids = {message.event_id for message in pending}
            self._stopped_for = frozenset(event_ids - self._waited if stops else ())
            if finish:
                self._waited |= event_ids
            self._checked, self._finish = pending, finish
            return finish

    def stopped_for(self, pending: tuple[QueuedMessage, ...]) -> frozenset[str]:
        """Return the messages of ``pending`` the judge said the reply cannot wait for, leaving out any it let wait."""
        return self._stopped_for if self._checked == pending else frozenset()

    def _sender_label(self, message: QueuedMessage) -> str:
        """Name the sender by role only, so no user ID reaches the judge."""
        if self.requester_id is None:
            return ""
        return ' from="requester"' if message.requester_id == self.requester_id else ' from="another person"'

    def _request(self, pending: tuple[QueuedMessage, ...]) -> JudgmentRequest | str:
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
            if message.visible_response is None or not clip_is_unredacted(message.visible_response):
                return "visible_response_unavailable"
            queued.append(
                f"<queued_message{self._sender_label(message)}>\n{message.text}\n</queued_message>\n"
                f"<visible_response>\n{clip_judgment_text(message.visible_response)}\n</visible_response>",
            )
        # Plain tags, not JSON: escaping changes redacted text, and the redaction check would refuse it.
        evidence = "\n".join([f"<active_request>\n{self.active_text}\n</active_request>", *queued])
        request = fit_judgment_request(
            MID_TURN_QUESTION,
            self.conversation_context,
            JudgmentMessage("user", evidence),
            instructions=self.instructions,
            max_context_messages=_MAX_CONTEXT_MESSAGES,
        )
        return "history_unjudgeable" if request is None else request
