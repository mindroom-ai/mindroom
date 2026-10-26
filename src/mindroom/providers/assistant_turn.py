"""What adapters share to build a turn: the answering model and its timings, and reasoning written inline.

Every assistant message a model answered is stored with its provenance (``llm/messages.py`` ``model_provenance``),
which usage accounting and a run's own model messages read; ``stamp_model`` is how an adapter records it on the turn
it finishes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import ModelIdentity, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["ThinkTagSplitter", "finished_assistant_message", "output_started", "stamp_finished_turn", "stamp_model"]

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def stamp_model(message: Message, identity: ModelIdentity) -> None:
    """Record on ``message`` which model answered it (``model_provenance``), keeping the rest of its provider data."""
    message.provider_data = {
        **(message.provider_data or {}),
        **model_provenance(identity.id, identity.provider_name),
    }


def stamp_finished_turn(
    message: Message,
    identity: ModelIdentity,
    *,
    started: float,
    first_output_at: float | None,
) -> Message:
    """Record which model answered ``message`` and how long the request took; ``started`` is a monotonic time."""
    stamp_model(message, identity)
    message.metrics.duration = time.monotonic() - started
    if first_output_at is not None:
        message.metrics.time_to_first_token = first_output_at - started
    return message


def output_started(first_output_at: float | None, event: StreamEvent) -> float | None:
    """Return when the turn's first output arrived: ``first_output_at``, or now when ``event`` is that output.

    Text, reasoning, and complete tool calls are output; usage reports and stream progress are not.
    """
    if first_output_at is None and isinstance(event, TextDelta | ReasoningDelta | ToolCallReady):
        return time.monotonic()
    return first_output_at


def finished_assistant_message(
    identity: ModelIdentity,
    *,
    content: str | None,
    reasoning: str | None = None,
    tool_calls: Sequence[dict[str, Any]],
    provider_data: dict[str, Any] | None = None,
    usage: MessageMetrics | None,
    started: float,
    first_output_at: float | None,
) -> Message:
    """Return the finished assistant message of one turn, stamped with its provenance and timings."""
    message = Message(
        role="assistant",
        content=content,
        reasoning_content=reasoning,
        tool_calls=list(tool_calls) or None,
        provider_data=provider_data,
    )
    if usage is not None:
        message.metrics = usage
    return stamp_finished_turn(message, identity, started=started, first_output_at=first_output_at)


@dataclass(slots=True)
class ThinkTagSplitter:
    """Split the reasoning a model writes inline, as a leading ``<think>`` block, from its reply text as it streams.

    A reply that opens with ``<think>``, after any whitespace, streams everything before ``</think>`` as reasoning,
    and a block the reply never closes is all reasoning; whitespace that starts the reasoning or the answer after it
    is dropped. Any other reply streams as it came. Text that could still open or close the block is held until a
    later piece, or the reply's end, decides it.
    """

    _state: Literal["opening", "reasoning", "answer"] = "opening"
    _held: str = ""
    # Whether whitespace that starts the reasoning or the answer is still being dropped.
    _trim: bool = False

    def add(self, text: str) -> list[TextDelta | ReasoningDelta]:
        """Return the reasoning and answer text that one piece of the reply's text decides."""
        if self._state == "answer" and not self._trim:
            return [TextDelta(text)] if text else []
        self._held += text
        return self._release(ended=False)

    def finish(self) -> list[TextDelta | ReasoningDelta]:
        """Return the text still held when the reply ends."""
        return self._release(ended=True)

    def _release(self, *, ended: bool) -> list[TextDelta | ReasoningDelta]:
        if self._state == "opening":
            opening = self._held.lstrip()
            if not opening.startswith(_THINK_OPEN):
                if _THINK_OPEN.startswith(opening) and not ended:
                    return []
                self._state = "answer"
                text = self._take(len(self._held))
                return [TextDelta(text)] if text else []
            self._state, self._held, self._trim = "reasoning", opening.removeprefix(_THINK_OPEN), True
        events: list[TextDelta | ReasoningDelta] = []
        if self._state == "reasoning":
            end = self._held.find(_THINK_CLOSE)
            closed = end != -1
            if not closed:
                end = len(self._held) - (0 if ended else _tag_prefix_length(self._held, _THINK_CLOSE))
            if reasoning := self._trimmed(self._take(end)):
                events.append(ReasoningDelta(reasoning))
            if not closed:
                return events
            self._take(len(_THINK_CLOSE))
            self._state, self._trim = "answer", True
        if answer := self._trimmed(self._take(len(self._held))):
            events.append(TextDelta(answer))
        return events

    def _take(self, length: int) -> str:
        taken, self._held = self._held[:length], self._held[length:]
        return taken

    def _trimmed(self, text: str) -> str:
        if self._trim:
            text = text.lstrip()
            self._trim = not text
        return text


def _tag_prefix_length(text: str, tag: str) -> int:
    """Return the length of the longest end of ``text`` that ``tag`` could continue."""
    for length in range(min(len(text), len(tag) - 1), 0, -1):
        if tag.startswith(text[-length:]):
            return length
    return 0
