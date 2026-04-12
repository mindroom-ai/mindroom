"""Memory prompt and conversation shaping helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

    from ._shared import MemoryResult

_USER_TURN_TIME_PREFIX_RE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2} [^\]]+\]\s")


def _format_memories_as_context(memories: list[MemoryResult], context_type: str = "agent") -> str:
    """Format memories into a context string."""
    if not memories:
        return ""

    context_parts = [
        f"[Automatically extracted {context_type} memories - may not be relevant to current context]",
        f"Previous {context_type} memories that might be related:",
    ]
    context_parts.extend(f"- {memory.get('memory', '')}" for memory in memories)
    return "\n".join(context_parts)


def strip_user_turn_time_prefix(text: str) -> str:
    """Remove bot-injected timestamp metadata from a user turn."""
    return _USER_TURN_TIME_PREFIX_RE.sub("", text, count=1)


def compose_current_turn_text(
    raw_prompt: str,
    tail_text: str | None = None,
) -> str:
    """Append one model-only tail block without duplicating the raw user prompt."""
    prompt_chunks: list[str] = []
    normalized_raw_prompt = raw_prompt.strip()
    normalized_tail_text = tail_text.strip() if tail_text else ""
    normalized_tail_without_time = strip_user_turn_time_prefix(normalized_tail_text) if normalized_tail_text else ""

    if normalized_raw_prompt:
        prompt_chunks.append(raw_prompt)
        if normalized_tail_text == normalized_raw_prompt:
            normalized_tail_text = ""
        elif normalized_tail_text.startswith(f"{normalized_raw_prompt}\n\n"):
            normalized_tail_text = normalized_tail_text[len(normalized_raw_prompt) + 2 :].lstrip()
        elif normalized_tail_without_time == normalized_raw_prompt:
            normalized_tail_text = ""
        elif normalized_tail_without_time.startswith(f"{normalized_raw_prompt}\n\n"):
            normalized_tail_text = normalized_tail_without_time[len(normalized_raw_prompt) + 2 :].lstrip()
    elif normalized_tail_text:
        prompt_chunks.append(normalized_tail_text)
        normalized_tail_text = ""

    if normalized_tail_text:
        prompt_chunks.append(normalized_tail_text)

    return "\n\n".join(chunk for chunk in prompt_chunks if chunk)


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
