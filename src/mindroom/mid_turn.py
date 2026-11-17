"""Bounded decisions about continuing work while human messages are queued."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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


MID_TURN_MODE_KEY = "io.mindroom.mid_turn"


class MidTurnMode(StrEnum):
    """What a person asks a message sent while an agent replies to do; a client that sends none gets Auto."""

    STEER = "steer"
    QUEUE = "queue"
    AUTO = "auto"


class MidTurnOutcome(StrEnum):
    """What the gate decided for a queue: it can wait, the judge said it cannot, or the reply pauses without a verdict."""

    WAIT = "wait"
    # Only the judge's explicit verdict; a judgment that failed, gave no answer, or was skipped is a pause.
    CANNOT_WAIT = "cannot_wait"
    PAUSE = "pause"


def _verdict_outcome(verdict: bool | None) -> MidTurnOutcome:
    """Read the judge's answer; no answer is a pause, never a "cannot wait"."""
    if verdict is True:
        return MidTurnOutcome.WAIT
    if verdict is False:
        return MidTurnOutcome.CANNOT_WAIT
    return MidTurnOutcome.PAUSE


def mid_turn_mode(content: Mapping[str, object] | None) -> MidTurnMode:
    """Read the mode a message's content names; missing, malformed, and unknown values are Auto."""
    # Event content comes from any client, so neither it nor the value is trusted to have the expected type.
    if not isinstance(content, Mapping):
        return MidTurnMode.AUTO
    value = content.get(MID_TURN_MODE_KEY)
    if not isinstance(value, str):
        return MidTurnMode.AUTO
    try:
        return MidTurnMode(value)
    except ValueError:
        return MidTurnMode.AUTO


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
    # The mode it was admitted with; an edit keeps it.
    mode: MidTurnMode = MidTurnMode.AUTO


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
    # Receives the messages, as the judge read them, the reply wraps up for.
    on_stop: Callable[[tuple[QueuedMessage, ...]], None] | None = None
    _acknowledged: set[str] = field(default_factory=set)
    _checked: tuple[QueuedMessage, ...] | None = None
    _outcome: MidTurnOutcome = MidTurnOutcome.PAUSE
    # Messages, as they read then, in a queue that could wait, and those this reply's verdicts say it cannot wait for.
    _waited: set[QueuedMessage] = field(default_factory=set)
    _stopping: set[QueuedMessage] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def bind_conversation_context(self, context: tuple[JudgmentMessage, ...] | None) -> None:
        """Freeze public history after lock acquisition, invalidating any earlier decision."""
        self.conversation_context = context
        self._checked = None
        self._outcome = MidTurnOutcome.PAUSE

    def record_visible_response(self, text: str, tool_trace: Sequence[ToolTraceEntry]) -> None:
        """Keep the latest Matrix-acknowledged text, with its tool calls as tags, for later queue snapshots."""
        # Without a trace, marker-shaped lines are the agent's own text.
        self.visible_response_text = body_with_tool_tags(text, tool_trace) if tool_trace else text

    async def acknowledge_queued(self, message: QueuedMessage) -> None:
        """Attempt one best-effort acknowledgement per message, once its caller accepts that it waits."""
        if self.on_defer is None or message.event_id in self._acknowledged:
            return
        self._acknowledged.add(message.event_id)
        try:
            await self.on_defer(message.event_id)
        except Exception:
            logger.warning("mid_turn_reaction_failed")

    def _between_people(self, message: QueuedMessage) -> bool:
        """Return whether another person than the requester wrote this message, mentioning nobody."""
        return self.requester_id is not None and message.mentions_nobody and message.requester_id != self.requester_id

    async def decide(
        self,
        pending: tuple[QueuedMessage, ...],
        *,
        current: Callable[[], bool],
        judge: bool = True,
    ) -> MidTurnOutcome:
        """Decide whether ``pending`` can wait for the active reply, acknowledging each message while ``current`` holds.

        Without ``judge``, ``pending`` cannot wait, whatever an earlier judgment of it said, and the judge is not asked.
        A queue that changes while it is acknowledged pauses the reply; callers still check their own snapshot.
        """
        try:
            outcome = await self._decide(pending, judge=judge)
        except Exception:
            # Callers hand off as on a failed judgment, so a judgment that raises never costs the active reply.
            outcome = MidTurnOutcome.PAUSE
        if outcome is not MidTurnOutcome.WAIT:
            return outcome
        for message in pending:
            # Matrix delivery can yield to a newer correction or a concurrent handoff.
            if not current():
                return MidTurnOutcome.PAUSE
            # Whether the agent answers another person's remark is decided after the reply, not promised here.
            if not self._between_people(message):
                await self.acknowledge_queued(message)
        return MidTurnOutcome.WAIT if current() else MidTurnOutcome.PAUSE

    async def _decide(self, pending: tuple[QueuedMessage, ...], *, judge: bool) -> MidTurnOutcome:
        """Let the queue wait only with sufficient confidence that no interruption is needed."""
        async with self._lock:
            if judge and self._checked == pending:
                return self._outcome
            outcome = MidTurnOutcome.PAUSE
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
                        outcome = _verdict_outcome(verdict)
                        reason = "no_answer" if verdict is None else None
                    # An evaluator can absorb its cancellation and still answer; a cancelled judgment commits nothing.
                    if (task := asyncio.current_task()) is not None and task.cancelling():
                        raise asyncio.CancelledError
            if reason is not None:
                # Unjudged, the reply wraps up, unless only other people's messages to nobody in particular wait.
                if judge and all(self._between_people(message) for message in pending):
                    outcome = MidTurnOutcome.WAIT
                logger.info(
                    "Mid-turn judgment skipped",
                    reason=reason,
                    queued_messages=len(pending),
                    outcome="waits" if outcome is MidTurnOutcome.WAIT else "wraps_up",
                )
            self._record_stopping(pending, outcome)
            # The release of a reply only waiting on jobs is no verdict, so it leaves the judge's "cannot wait" on the
            # same queue standing, while a queue that could wait then pauses from now on.
            if judge or self._checked != pending or self._outcome is not MidTurnOutcome.CANNOT_WAIT:
                self._checked, self._outcome = pending, outcome
            return outcome

    def _record_stopping(self, pending: tuple[QueuedMessage, ...], outcome: MidTurnOutcome) -> None:
        """Keep, per reply, the messages its verdicts let wait and those they say it cannot wait for."""
        if outcome is MidTurnOutcome.WAIT:
            self._waited.update(pending)
            self._stopping.difference_update(pending)
        elif outcome is MidTurnOutcome.CANNOT_WAIT and self._stopping.isdisjoint(pending):
            # One verdict covers the whole queue, so it names the messages the judge has not let wait (as they read
            # then, so an edit of one can count) only when one person sent them all and no earlier verdict's
            # message, still waiting, already explains it.
            fresh = [message for message in pending if message not in self._waited]
            if len({message.requester_id for message in fresh}) == 1:
                self._stopping.update(fresh)

    def report_stop(self) -> None:
        """Hand ``on_stop`` the messages, as the judge read them, that this reply's verdicts say it cannot wait for."""
        if self.on_stop is not None and self._stopping:
            self.on_stop(tuple(self._stopping))

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
