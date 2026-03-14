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
    append_agent_daily_file_memory,
    delete_file_agent_memory,
    get_file_agent_memory,
    list_file_agent_memories,
    load_scope_entrypoint_context,
    search_file_agent_memories,
    store_file_conversation_memory,
    update_file_agent_memory,
)
from ._mem0_backend import (
    add_mem0_agent_memory,
    delete_mem0_agent_memory,
    get_mem0_agent_memory,
    list_mem0_agent_memories,
    search_mem0_agent_memories,
    store_mem0_conversation_memory,
    update_mem0_agent_memory,
)
from ._policy import (
    agent_scope_user_id,
    caller_uses_file_memory_backend,
    resolve_file_memory_resolution,
    team_uses_file_memory_backend,
    use_file_memory_backend,
)
from ._prompting import (
    _format_memories_as_context,
    build_memory_messages,
)
from ._shared import MemoryResult, new_memory_id

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


async def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> str:
    """Build a prompt enhanced with relevant memories."""
    logger.debug("Building enhanced prompt", agent=agent_name)
    agent_memories = await search_agent_memories(prompt, agent_name, storage_path, config)
    if agent_memories:
        logger.debug("Agent memories added", count=len(agent_memories))

    if use_file_memory_backend(config, agent_name=agent_name):
        resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
        agent_entrypoint = load_scope_entrypoint_context(agent_scope_user_id(agent_name), resolution, config)
        context_chunks: list[str] = []
        if agent_entrypoint:
            context_chunks.append(f"[File memory entrypoint (agent)]\n{agent_entrypoint}")
        if agent_memories:
            context_chunks.append(_format_memories_as_context(agent_memories, "agent file"))
        if context_chunks:
            return f"{'\n\n'.join(context_chunks)}\n\n{prompt}"
        return prompt

    if not agent_memories:
        return prompt
    return f"{_format_memories_as_context(agent_memories, 'agent')}\n\n{prompt}"


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    thread_history: list[dict] | None = None,
    user_id: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Store conversation in memory for future recall."""
    if not prompt:
        return

    with tool_execution_identity(execution_identity or get_tool_execution_identity()):
        use_file_backend = (
            use_file_memory_backend(config, agent_name=agent_name)
            if isinstance(agent_name, str)
            else team_uses_file_memory_backend(config, agent_name)
        )
        if use_file_backend:
            store_file_conversation_memory(prompt, agent_name, storage_path, config)
            return

        messages = build_memory_messages(prompt, thread_history, user_id)
        await store_mem0_conversation_memory(
            messages,
            agent_name,
            storage_path,
            session_id,
            config,
            replica_key=new_memory_id() if isinstance(agent_name, list) else None,
            create_memory=create_memory_instance,
        )
