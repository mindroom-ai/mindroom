"""Memory prompt and conversation shaping helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mindroom.prompt_templates import render_prompt_template

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

    from ._shared import MemoryResult

_USER_TURN_TIME_PREFIX_RE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2} [^\]]+\]\s")


def format_memories_as_context(
    memories: list[MemoryResult],
    context_type: str = "agent",
    *,
    prompt_template: str,
) -> str:
    """Format memories into a context string."""
    if not memories:
        return ""

    memory_lines = "\n".join(f"- {memory.get('memory', '')}" for memory in memories)
    return render_prompt_template(prompt_template, context_type=context_type, memory_lines=memory_lines)


def strip_user_turn_time_prefix(text: str) -> str:
    """Remove bot-injected timestamp metadata from a user turn."""
    return _USER_TURN_TIME_PREFIX_RE.sub("", text, count=1)


def _build_conversation_messages(
    thread_history: Sequence[ResolvedVisibleMessage],
    current_prompt: str,
    user_id: str,
) -> list[dict]:
    messages: list[dict] = []
    for message in thread_history:
        role = "user" if message.sender == user_id else "assistant"
        body = message.body.strip()
        if not body:
            continue
        messages.append({"role": role, "content": body})
    messages.append({"role": "user", "content": current_prompt})
    return messages


def build_memory_messages(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user_id: str | None,
) -> list[dict]:
    """Convert prompt and optional thread history into memory-save messages."""
    if thread_history and user_id:
        return _build_conversation_messages(thread_history, prompt, user_id)
    return [{"role": "user", "content": prompt}]
