"""Memory prompt and conversation shaping helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .shared import MemoryResult


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


def build_prompt_with_memories(
    prompt: str,
    *,
    agent_memories: list[MemoryResult],
    room_memories: list[MemoryResult] | None = None,
) -> str:
    """Prefix a prompt with agent and room memory context."""
    enhanced_prompt = prompt
    if agent_memories:
        enhanced_prompt = f"{_format_memories_as_context(agent_memories, 'agent')}\n\n{prompt}"
    if room_memories:
        enhanced_prompt = f"{_format_memories_as_context(room_memories, 'room')}\n\n{enhanced_prompt}"
    return enhanced_prompt


def build_file_prompt_with_memory_context(
    prompt: str,
    *,
    agent_entrypoint: str,
    agent_memories: list[MemoryResult],
    room_entrypoint: str = "",
    room_memories: list[MemoryResult] | None = None,
) -> str:
    """Prefix a prompt with file-memory entrypoints and search hits."""
    context_chunks: list[str] = []
    if agent_entrypoint:
        context_chunks.append(f"[File memory entrypoint (agent)]\n{agent_entrypoint}")
    if agent_memories:
        context_chunks.append(_format_memories_as_context(agent_memories, "agent file"))
    if room_entrypoint:
        context_chunks.append(f"[File memory entrypoint (room)]\n{room_entrypoint}")
    if room_memories:
        context_chunks.append(_format_memories_as_context(room_memories, "room file"))
    if context_chunks:
        return f"{'\n\n'.join(context_chunks)}\n\n{prompt}"
    return prompt


def _build_conversation_messages(
    thread_history: list[dict],
    current_prompt: str,
    user_id: str,
) -> list[dict]:
    messages: list[dict] = []
    for message in thread_history:
        body = message.get("body", "").strip()
        if not body:
            continue
        role = "user" if message.get("sender", "") == user_id else "assistant"
        messages.append({"role": role, "content": body})
    messages.append({"role": "user", "content": current_prompt})
    return messages


def build_memory_messages(prompt: str, thread_history: list[dict] | None, user_id: str | None) -> list[dict]:
    """Convert prompt and optional thread history into memory-save messages."""
    if thread_history and user_id:
        return _build_conversation_messages(thread_history, prompt, user_id)
    return [{"role": "user", "content": prompt}]
