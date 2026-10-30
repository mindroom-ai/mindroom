"""What adapters share to build a turn: its timings and stream progress, the events that end it, and inline reasoning.

Every assistant message a model answered is stored with its provenance (``llm/messages.py`` ``model_provenance``),
which usage accounting and a run's own model messages read; ``stamp_model`` is how an adapter records it on the turn
it finishes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import ReasoningDelta, StreamProgress, TextDelta, ToolCallReady, TurnCompleted, UsageReported

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.llm.provider import ModelIdentity, StopReason, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["ThinkTagSplitter", "TurnTimer", "finished_turn", "stamp_finished_turn", "stamp_model", "turn_end"]

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def stamp_model(message: Message, identity: ModelIdentity) -> None:
    """Record on ``message`` which model answered it (``model_provenance``), keeping the rest of its provider data."""
    message.provider_data = {
        **(message.provider_data or {}),
        **model_provenance(identity.id, identity.provider_name),
    }


@dataclass(slots=True)
class TurnTimer:
    """When one turn's request started and when its first output arrived, as monotonic times.

    Text, reasoning, and complete tool calls are output; usage reports and stream progress are not. An adapter passes
    every output event it yields through ``output`` or ``chunk``, including the tool calls it completes only after its
    stream ends.
    """

    started: float = field(default_factory=time.monotonic)
    first_output_at: float | None = None

    def output(self, event: StreamEvent) -> StreamEvent:
        """Return ``event``, noting when it is the turn's first output."""
        if self.first_output_at is None and isinstance(event, TextDelta | ReasoningDelta | ToolCallReady):
            self.first_output_at = time.monotonic()
        return event

    def chunk(self, events: Sequence[StreamEvent]) -> Iterator[StreamEvent]:
        """Yield the events one provider chunk carried, noting its output, or progress when it carried none.

        A chunk without events, such as a tool-call argument piece or a hosted tool's progress, still shows that
        the provider is working.
        """
        for event in events or [StreamProgress()]:
            yield self.output(event)


def stamp_finished_turn(message: Message, identity: ModelIdentity, timer: TurnTimer) -> None:
    """Record which model answered ``message`` and how long the request took."""
    stamp_model(message, identity)
    message.metrics.duration = time.monotonic() - timer.started
    if timer.first_output_at is not None:
        message.metrics.time_to_first_token = timer.first_output_at - timer.started


def finished_turn(
    identity: ModelIdentity,
    timer: TurnTimer,
    *,
    content: str | None,
    reasoning: str | None = None,
    tool_calls: Sequence[dict[str, Any]],
    provider_data: dict[str, Any] | None = None,
    usage: MessageMetrics | None,
    stop_reason: StopReason | None,
) -> list[StreamEvent]:
    """Return the events that end one finished turn, its assistant message stamped with its provenance and timings."""
    message = Message(
        role="assistant",
        content=content,
        reasoning_content=reasoning,
        tool_calls=list(tool_calls) or None,
        provider_data=provider_data,
    )
    if usage is not None:
        message.metrics = usage
    stamp_finished_turn(message, identity, timer)
    return turn_end(message, stop_reason)


def turn_end(message: Message, stop_reason: StopReason | None) -> list[StreamEvent]:
    """Return a finished turn's last events: its final usage report, when the provider reported usage, then completion.

    The final report carries the message's metrics, timings included.
    """
    completed = TurnCompleted(message=message, stop_reason=stop_reason)
    return [UsageReported(message.metrics), completed] if message.metrics.reported else [completed]


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
