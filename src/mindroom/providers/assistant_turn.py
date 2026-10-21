# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""What every adapter does to finish a turn: record the answering model and timings, split ``<think>`` reasoning.

The ``mindroom_model`` provenance key is persisted with every assistant message and read by usage accounting,
so each adapter stamps it through one function.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.usage import MessageMetrics

__all__ = [
    "MODEL_PROVENANCE_KEY",
    "finished_assistant_message",
    "one_message_per_result",
    "split_thinking",
    "stamp_finished_turn",
]

MODEL_PROVENANCE_KEY = "mindroom_model"


def stamp_finished_turn(
    message: Message,
    identity: ModelIdentity,
    *,
    started: float,
    first_output_at: float | None,
) -> Message:
    """Record which model answered ``message`` and how long the request took; ``started`` is a monotonic time."""
    message.provider_data = {
        **(message.provider_data or {}),
        MODEL_PROVENANCE_KEY: {"id": identity.id, "provider": identity.provider_name},
    }
    message.metrics.duration = time.monotonic() - started
    if first_output_at is not None:
        message.metrics.time_to_first_token = first_output_at - started
    return message


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


def split_thinking(content: str) -> tuple[str | None, str]:
    """Split ``<think>`` reasoning from a reply that arrived whole, as Agno's ``extract_thinking_content`` callers did.

    A missing opening tag means the reasoning starts the reply. Without a closing tag, or when the reasoning is
    empty, the reply comes back unchanged with no reasoning, so an empty ``<think></think>`` block stays in the text.
    """
    end = content.find("</think>")
    if end == -1:
        return None, content
    start = content.find("<think>")
    reasoning = (content[:end] if start == -1 else content[start + len("<think>") : end]).strip()
    if not reasoning:
        return None, content
    return reasoning, content[end + len("</think>") :].strip()


def one_message_per_result(messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
    """Append each tool result as its own stored message; wire formats merge them where a provider wants one turn."""
    del messages
    return list(results)
