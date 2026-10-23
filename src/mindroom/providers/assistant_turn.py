"""What every adapter does to finish a turn: record the answering model and its timings.

Every assistant message a model answered is stored with its provenance (``llm/messages.py`` ``model_provenance``),
which usage accounting and a run's own model messages read; ``stamp_model`` is how an adapter records it on the turn
it finishes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import ModelIdentity, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = [
    "finished_assistant_message",
    "output_started",
    "stamp_finished_turn",
    "stamp_model",
]


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
        provider_data=dict(provider_data or {}),
    )
    if usage is not None:
        message.metrics = usage
    return stamp_finished_turn(message, identity, started=started, first_output_at=first_output_at)
