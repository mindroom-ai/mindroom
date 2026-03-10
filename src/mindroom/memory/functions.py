"""Public memory API and orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.memory.config import create_memory_instance
from mindroom.tool_system.worker_routing import (
    get_tool_execution_identity,
    tool_execution_identity,
)

from ._file_backend import (
    add_file_agent_memory,
    add_file_room_memory,
    append_agent_daily_file_memory,
    delete_file_agent_memory,
    get_file_agent_memory,
    list_file_agent_memories,
    load_scope_entrypoint_context,
    search_file_agent_memories,
    search_file_room_memories,
    store_file_conversation_memory,
    update_file_agent_memory,
)
from ._mem0_backend import (
    add_mem0_agent_memory,
    add_mem0_room_memory,
    delete_mem0_agent_memory,
    get_mem0_agent_memory,
    list_mem0_agent_memories,
    search_mem0_agent_memories,
    search_mem0_room_memories,
    store_mem0_conversation_memory,
    update_mem0_agent_memory,
)
from ._policy import (
    caller_uses_file_memory_backend,
    resolve_file_memory_resolution,
    team_uses_file_memory_backend,
    use_file_memory_backend,
)
from ._shared import FileMemoryResolution, MemoryResult, new_memory_id

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


async def add_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    metadata: dict | None = None,
) -> None:
    """Add a memory for an agent."""
    if use_file_memory_backend(config, agent_name=agent_name):
        add_file_agent_memory(content, agent_name, storage_path, config)
        return
    await add_mem0_agent_memory(
        content,
        agent_name,
        storage_path,
        config,
        metadata=metadata,
        create_memory=create_memory_instance,
    )


def append_agent_daily_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    preserve_resolved_storage_path: bool = False,
) -> MemoryResult:
    """Append one memory entry to today's per-agent daily memory file."""
    return append_agent_daily_file_memory(
        content,
        agent_name,
        storage_path,
        config,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )


async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search agent memories including team memories."""
    if use_file_memory_backend(config, agent_name=agent_name):
        return search_file_agent_memories(query, agent_name, storage_path, config, limit=limit)
    return await search_mem0_agent_memories(
        query,
        agent_name,
        storage_path,
        config,
        limit=limit,
        create_memory=create_memory_instance,
    )


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 100,
    *,
    preserve_resolved_storage_path: bool = False,
) -> list[MemoryResult]:
    """List all memories for an agent."""
    if use_file_memory_backend(config, agent_name=agent_name):
        return list_file_agent_memories(
            agent_name,
            storage_path,
            config,
            limit=limit,
            preserve_resolved_storage_path=preserve_resolved_storage_path,
        )
    return await list_mem0_agent_memories(
        agent_name,
        storage_path,
        config,
        limit=limit,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
        create_memory=create_memory_instance,
    )


async def get_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    """Get a single memory by ID."""
    if caller_uses_file_memory_backend(config, caller_context):
        return get_file_agent_memory(memory_id, caller_context, storage_path, config)
    return await get_mem0_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        create_memory=create_memory_instance,
    )


async def update_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    """Update a single memory by ID."""
    if caller_uses_file_memory_backend(config, caller_context):
        update_file_agent_memory(memory_id, content, caller_context, storage_path, config)
        return
    await update_mem0_agent_memory(
        memory_id,
        content,
        caller_context,
        storage_path,
        config,
        create_memory=create_memory_instance,
    )


async def delete_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    """Delete a single memory by ID."""
    if caller_uses_file_memory_backend(config, caller_context):
        delete_file_agent_memory(memory_id, caller_context, storage_path, config)
        return
    await delete_mem0_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        create_memory=create_memory_instance,
    )


async def add_room_memory(
    content: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    agent_name: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Add a memory for a room."""
    if use_file_memory_backend(config, agent_name=agent_name):
        add_file_room_memory(content, room_id, storage_path, config, agent_name=agent_name)
        return
    await add_mem0_room_memory(
        content,
        room_id,
        storage_path,
        config,
        agent_name=agent_name,
        metadata=metadata,
        create_memory=create_memory_instance,
    )


async def search_room_memories(
    query: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    agent_name: str | None = None,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search room memories."""
    if use_file_memory_backend(config, agent_name=agent_name):
        return search_file_room_memories(
            query,
            room_id,
            storage_path,
            config,
            agent_name=agent_name,
            limit=limit,
        )
    return await search_mem0_room_memories(
        query,
        room_id,
        storage_path,
        config,
        agent_name=agent_name,
        limit=limit,
        create_memory=create_memory_instance,
    )


def format_memories_as_context(memories: list[MemoryResult], context_type: str = "agent") -> str:
    """Format memories into a context string."""
    if not memories:
        return ""

    context_parts = [
        f"[Automatically extracted {context_type} memories - may not be relevant to current context]",
        f"Previous {context_type} memories that might be related:",
    ]
    context_parts.extend(f"- {memory.get('memory', '')}" for memory in memories)
    return "\n".join(context_parts)


async def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    room_id: str | None = None,
) -> str:
    """Build a prompt enhanced with relevant memories."""
    resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
    logger.debug("Building enhanced prompt", agent=agent_name)
    if use_file_memory_backend(config, agent_name=agent_name):
        return await _build_file_memory_enhanced_prompt(
            prompt,
            agent_name,
            storage_path,
            resolution,
            config,
            room_id,
        )

    enhanced_prompt = prompt
    agent_memories = await search_agent_memories(prompt, agent_name, storage_path, config)
    if agent_memories:
        enhanced_prompt = f"{format_memories_as_context(agent_memories, 'agent')}\n\n{prompt}"
        logger.debug("Agent memories added", count=len(agent_memories))

    if room_id:
        room_memories = await search_room_memories(
            prompt,
            room_id,
            storage_path,
            config,
            agent_name=agent_name,
        )
        if room_memories:
            enhanced_prompt = f"{format_memories_as_context(room_memories, 'room')}\n\n{enhanced_prompt}"
            logger.debug("Room memories added", count=len(room_memories))

    return enhanced_prompt


async def _build_file_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    base_storage_path: Path,
    resolution: FileMemoryResolution,
    config: Config,
    room_id: str | None,
) -> str:
    context_chunks: list[str] = []

    agent_entrypoint = load_scope_entrypoint_context(f"agent_{agent_name}", resolution, config)
    if agent_entrypoint:
        context_chunks.append(f"[File memory entrypoint (agent)]\n{agent_entrypoint}")

    agent_memories = await search_agent_memories(prompt, agent_name, base_storage_path, config)
    if agent_memories:
        context_chunks.append(format_memories_as_context(agent_memories, "agent file"))

    if room_id:
        safe_room_id = room_id.replace(":", "_").replace("!", "")
        room_entrypoint = load_scope_entrypoint_context(f"room_{safe_room_id}", resolution, config)
        if room_entrypoint:
            context_chunks.append(f"[File memory entrypoint (room)]\n{room_entrypoint}")

        room_memories = await search_room_memories(
            prompt,
            room_id,
            base_storage_path,
            config,
            agent_name=agent_name,
        )
        if room_memories:
            context_chunks.append(format_memories_as_context(room_memories, "room file"))

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


def _build_memory_messages(prompt: str, thread_history: list[dict] | None, user_id: str | None) -> list[dict]:
    if thread_history and user_id:
        return _build_conversation_messages(thread_history, prompt, user_id)
    return [{"role": "user", "content": prompt}]


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    room_id: str | None = None,
    thread_history: list[dict] | None = None,
    user_id: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Store conversation in memory for future recall."""
    if not prompt:
        return

    with tool_execution_identity(execution_identity or get_tool_execution_identity()):
        messages = _build_memory_messages(prompt, thread_history, user_id)
        use_file_backend = (
            use_file_memory_backend(config, agent_name=agent_name)
            if isinstance(agent_name, str)
            else team_uses_file_memory_backend(config, agent_name)
        )
        if use_file_backend:
            store_file_conversation_memory(prompt, agent_name, storage_path, config, room_id)
            return

        await store_mem0_conversation_memory(
            messages,
            agent_name,
            storage_path,
            session_id,
            config,
            room_id,
            replica_key=new_memory_id() if isinstance(agent_name, list) else None,
            create_memory=create_memory_instance,
        )
