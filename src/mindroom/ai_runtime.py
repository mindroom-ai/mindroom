"""The notice a reply's loop gives its model when a newer message waits, and the empty-response notice."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mindroom.llm.messages import QUEUED_MESSAGE_NOTICE_MARKER_KEY, Message, is_queued_notice_message
from mindroom.logging_config import get_logger
from mindroom.mid_turn import MidTurnOutcome

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
    "told_to_pause",
]

logger = get_logger(__name__)


_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"

EMPTY_RESPONSE_NOTICE = "The model returned an empty response — please try again."


class _SupportsQueuedMessageState(Protocol):
    """The messages waiting for a reply; Queue ones never interrupt it, so they get no notice and no judgment."""

    def has_interrupting_messages(self) -> bool: ...

    def interrupting_snapshot(self) -> tuple[QueuedMessage, ...]: ...

    def queued_snapshot(self) -> tuple[QueuedMessage, ...]: ...

    def requester_steer_snapshot(self) -> tuple[QueuedMessage, ...]: ...


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
) -> Generator[_QueuedMessageNoticeContext]:
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
    waiting = notice_context.state is not None and notice_context.state.has_interrupting_messages()
    stored = next(
        (
            index
            for index, message in enumerate(messages)
            if message.from_history and _is_response_notice(message, response_turn_id)
        ),
        None,
    )
    if stored is not None:
        # This request replays a copy of this response's notice that an earlier run under the same notice context
        # stored, such as the run a model switch stopped or a subagent's earlier turn that a follow-up delegation in the
        # same reply replays. That copy stands for the notice: it moves last while a newer message waits and stays
        # history, so it is stored once.
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


def told_to_pause(messages: Sequence[Message]) -> bool:
    """Return whether the request ``messages[-1]`` answers carried this reply's notice, and its newer message waits.

    Only a notice after that request's last assistant message counts, so a run that never read the notice, such as a
    subagent of a paused batch or a run whose queue emptied, is not told to pause by another run's notice or by history.
    """
    context = _queued_message_notice_context.get()
    if context is None or context.state is None or not context.state.has_interrupting_messages():
        return False
    for message in reversed(messages[:-1]):
        if message.role == "assistant":
            return False
        if _is_response_notice(message, context.response_turn_id):
            return True
    return False


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


def _requester_steers(context: _QueuedMessageNoticeContext) -> bool:
    return context.state is not None and bool(context.state.requester_steer_snapshot())


async def _judge_queued_notice(
    messages: list[Message],
    *,
    notice_text: str,
    steering: bool = False,
) -> bool | None:
    """Judge the waiting messages after a completed batch; return True when the run ends at that batch.

    A ``steering`` run owns its reply: the requester's Steer ends it unannounced and unjudged, and so does the judge's
    "cannot wait" on the queue as it still waits, so the message runs next as its own turn. Any other wrap-up gets the
    pause notice. Each look follows the awaited reactions and judgment, so an edited or removed message ends nothing
    for the verdict on its old text, while a message appended meanwhile leaves the judged queue's verdict standing.
    """
    context = _queued_message_notice_context.get()
    if context is None or context.mid_turn_gate is None or context.state is None:
        return None
    state = context.state
    gate = context.mid_turn_gate
    for message in state.queued_snapshot():
        await gate.acknowledge_queued(message)
    if steering and _requester_steers(context):
        gate.report_stop()
        return True
    pending = state.interrupting_snapshot()
    if not pending:
        return None
    if not context.notice_fired:
        outcome = await gate.decide(
            pending,
            current=lambda: not context.notice_fired and pending == state.interrupting_snapshot(),
        )
        steered = steering and _requester_steers(context)
        if outcome is MidTurnOutcome.WAIT and not steered:
            return None
        # The reply wraps up here, for a "cannot wait" and a pause alike, also when a newer message arrived during the
        # judgment, so a later verdict on a reply that kept waiting cannot leave a mark behind.
        gate.report_stop()
        if steered or (
            steering and outcome is MidTurnOutcome.CANNOT_WAIT and set(pending) <= set(state.interrupting_snapshot())
        ):
            return True
    _append_queued_notice_if_needed(messages=messages, notice_text=notice_text, judged=True)
    return None


def queued_notice_after_tool_batch(notice_text: str, *, steering: bool = False) -> AfterToolBatchHook:
    """Return the loop hook that appends the queued-message notice after a tool batch when a newer message waits.

    Without a mid-turn judge the notice follows every batch while messages are queued; with one, the judge decides
    after each completed batch. Calls paused for approval or delegation leave the batch unresolved; the response that
    resumes it judges the completed batch, reusing this turn's decision for an unchanged queue. A ``steering`` run,
    one that owns its reply, ends at a completed batch instead of being told to pause when its requester steers or the
    judge says the queue cannot wait; a delegated child or a team member does not, so it leaves that to its owner.
    """

    async def after_tool_batch(messages: list[Message], _results: Sequence[Message]) -> bool | None:
        context = _queued_message_notice_context.get()
        if context is None:
            return None
        completed = _ends_with_completed_tool_batch(messages, context.response_turn_id, after_tool_batch=True)
        if completed and context.mid_turn_gate is not None:
            return await _judge_queued_notice(messages, notice_text=notice_text, steering=steering)
        if completed and steering and _requester_steers(context):
            return True
        _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)
        return None

    return after_tool_batch


def queued_notice_on_resume_entry(notice_text: str, *, steering: bool = False) -> AfterToolBatchHook:
    """Return the loop hook that notifies a resumed run of a queued message after its completed tool batch.

    A ``steering`` run ends at that batch for its requester's Steer or the judge's "cannot wait" instead.
    """

    async def on_resume_entry(messages: list[Message], _results: Sequence[Message]) -> bool | None:
        context = _queued_message_notice_context.get()
        if context is None or not _ends_with_completed_tool_batch(messages, context.response_turn_id):
            return None
        if context.mid_turn_gate is not None:
            return await _judge_queued_notice(messages, notice_text=notice_text, steering=steering)
        if steering and _requester_steers(context):
            return True
        if context.state is not None and context.state.has_interrupting_messages():
            _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)
        return None

    return on_resume_entry
