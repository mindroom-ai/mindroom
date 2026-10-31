"""The notice a reply's loop gives its model when a newer message waits, and the empty-response notice."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY, is_queued_notice_message
from mindroom.llm.messages import Message
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from mindroom.agent_loop.model_loop import AfterToolBatchHook
    from mindroom.judgment.state import JudgmentMessage
    from mindroom.mid_turn import MidTurnGate, QueuedMessage

__all__ = [
    "EMPTY_RESPONSE_NOTICE",
    "bind_mid_turn_conversation_context",
    "queued_message_signal_context",
    "queued_notice_after_tool_batch",
    "queued_notice_on_resume_entry",
]

logger = get_logger(__name__)


_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"

EMPTY_RESPONSE_NOTICE = "The model returned an empty response — please try again."


class _SupportsQueuedMessageState(Protocol):
    def has_pending_human_messages(self) -> bool: ...

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]: ...


@dataclass
class _QueuedMessageNoticeContext:
    state: _SupportsQueuedMessageState | None
    mid_turn_gate: MidTurnGate | None = None
    response_turn_id: str = field(default_factory=lambda: str(uuid4()))
    notice_fired: bool = False


_queued_message_notice_context: ContextVar[_QueuedMessageNoticeContext | None] = ContextVar(
    "queued_message_notice_context",
    default=None,
)


@contextmanager
def queued_message_signal_context(
    signal: _SupportsQueuedMessageState | None,
    *,
    mid_turn_gate: MidTurnGate | None = None,
) -> Generator[_QueuedMessageNoticeContext, None, None]:
    """Bind one queued-message signal to the current async task."""
    notice_context = _QueuedMessageNoticeContext(state=signal, mid_turn_gate=mid_turn_gate)
    token = _queued_message_notice_context.set(notice_context)
    try:
        yield notice_context
    finally:
        _queued_message_notice_context.reset(token)


def bind_mid_turn_conversation_context(
    context_factory: Callable[[], tuple[JudgmentMessage, ...] | None],
) -> None:
    """Build refreshed public context only when this task has an active judge."""
    notice = _queued_message_notice_context.get()
    if notice is not None and notice.mid_turn_gate is not None:
        notice.mid_turn_gate.bind_conversation_context(context_factory())


def _is_response_notice(message: Message, response_turn_id: str) -> bool:
    """Return whether one message is the queued-message notice that response ``response_turn_id`` appended."""
    return (
        is_queued_notice_message(message)
        and (message.provider_data or {}).get(_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY) == response_turn_id
    )


def _append_queued_notice_if_needed(
    *,
    messages: list[Message],
    notice_text: str,
    judged: bool = False,
) -> None:
    notice_context = _queued_message_notice_context.get()
    if notice_context is None or (notice_context.mid_turn_gate is not None and not judged):
        return
    response_turn_id = notice_context.response_turn_id
    waiting = notice_context.state is not None and notice_context.state.has_pending_human_messages()
    stored = next(
        (
            index
            for index, message in enumerate(messages)
            if message.from_history and _is_response_notice(message, response_turn_id)
        ),
        None,
    )
    if stored is not None:
        # A model switch continued this response in a new run: the run it stopped stored the notice, which the new
        # run's history replays. That copy moves last while a newer message waits and stays history, so the response
        # stores its notice once.
        if waiting:
            messages.append(messages.pop(stored))
        return
    messages[:] = [message for message in messages if not _is_response_notice(message, response_turn_id)]
    if not waiting:
        return
    messages.append(
        Message(
            role="user",
            content=notice_text,
            provider_data={
                QUEUED_MESSAGE_NOTICE_MARKER_KEY: True,
                _QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id,
            },
        ),
    )
    if not notice_context.notice_fired:
        notice_context.notice_fired = True
        logger.info("queued_message_notice_injected", response_turn_id=response_turn_id)


def _ends_with_completed_tool_batch(
    messages: list[Message],
    response_turn_id: str,
    *,
    after_tool_batch: bool = False,
) -> bool:
    """Return whether the messages end with a complete tool batch, found without crossing a real user message."""
    results: list[Message] = []
    for message in reversed(messages):
        if _is_response_notice(message, response_turn_id):
            continue
        # Formatting may append user-role media after a just-completed batch, and a paused call's
        # result follows that media when the run resumes. Only a trailing user message is real input,
        # so at response entry it still stops this scan.
        if message.role == "user" and (after_tool_batch or results):
            continue
        if message.role == "tool":
            results.append(message)
            continue
        if message.role == "assistant" and message.tool_calls:
            call_ids = {call.get("id") for call in message.tool_calls}
            result_ids = {result.tool_call_id for result in results}
            return None not in call_ids and call_ids <= result_ids
        return False
    return False


async def _judge_queued_notice(messages: list[Message], *, notice_text: str) -> None:
    context = _queued_message_notice_context.get()
    if context is None or context.mid_turn_gate is None or context.state is None:
        return
    state = context.state
    pending = state.pending_message_snapshot()
    if not pending:
        return
    if not context.notice_fired and await context.mid_turn_gate.defers(
        pending,
        current=lambda: not context.notice_fired and pending == state.pending_message_snapshot(),
    ):
        return
    _append_queued_notice_if_needed(messages=messages, notice_text=notice_text, judged=True)


def queued_notice_after_tool_batch(notice_text: str) -> AfterToolBatchHook:
    """Return the loop hook that appends the queued-message notice after a tool batch when a newer message waits.

    Without a mid-turn judge the notice follows every batch while messages are queued; with one, the judge decides
    after each completed batch. Calls paused for approval or delegation leave the batch unresolved; the response that
    resumes it judges the completed batch, reusing this turn's decision for an unchanged queue.
    """

    async def after_tool_batch(messages: list[Message], _results: Sequence[Message]) -> None:
        _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)
        context = _queued_message_notice_context.get()
        if context is None or context.mid_turn_gate is None:
            return
        if not _ends_with_completed_tool_batch(messages, context.response_turn_id, after_tool_batch=True):
            return
        await _judge_queued_notice(messages, notice_text=notice_text)

    return after_tool_batch


def queued_notice_on_resume_entry(notice_text: str) -> AfterToolBatchHook:
    """Return the loop hook that notifies a resumed run of a queued message after its completed tool batch."""

    async def on_resume_entry(messages: list[Message], _results: Sequence[Message]) -> None:
        context = _queued_message_notice_context.get()
        if context is None or not _ends_with_completed_tool_batch(messages, context.response_turn_id):
            return
        if context.mid_turn_gate is not None:
            await _judge_queued_notice(messages, notice_text=notice_text)
        elif context.state is not None and context.state.has_pending_human_messages():
            _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)

    return on_resume_entry
