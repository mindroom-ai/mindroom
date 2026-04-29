"""Public memory API and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.memory.config import create_memory_instance
from mindroom.timing import timed

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
    caller_uses_disabled_memory_backend,
    caller_uses_file_memory_backend,
    resolve_file_memory_resolution,
    team_uses_disabled_memory_backend,
    team_uses_file_memory_backend,
    use_disabled_memory_backend,
    use_file_memory_backend,
)
from ._prompting import (
    _format_memories_as_context,
    build_memory_messages,
)
from ._shared import MemoryResult, new_memory_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._shared import ScopedMemoryCrud

logger = get_logger(__name__)


@dataclass(frozen=True)
class MemoryPromptParts:
    """Stable and turn-local prompt fragments used by the AI layer."""

    session_preamble: str = ""
    turn_context: str = ""


def _create_memory_factory(
    runtime_paths: RuntimePaths,
) -> Callable[..., Awaitable[ScopedMemoryCrud]]:
    return partial(create_memory_instance, runtime_paths=runtime_paths)


@timed("system_prompt_assembly.memory_search.file_backend")
def _search_file_backend_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int,
    execution_identity: ToolExecutionIdentity | None,
    timing_scope: str | None,
) -> list[MemoryResult]:
    return search_file_agent_memories(
        query,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        limit=limit,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )


@timed("system_prompt_assembly.memory_search.mem0_backend")
async def _search_mem0_backend_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int,
    execution_identity: ToolExecutionIdentity | None,
    timing_scope: str | None,
) -> list[MemoryResult]:
    return await search_mem0_agent_memories(
        query,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        limit=limit,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )


@timed("system_prompt_assembly.memory_file_entrypoint_load")
def _load_agent_file_entrypoint_context(
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    timing_scope: str | None,
) -> str:
    resolution = resolve_file_memory_resolution(
        storage_path,
        config,
        runtime_paths,
        agent_name=agent_name,
        execution_identity=execution_identity,
    )
    return load_scope_entrypoint_context(
        agent_scope_user_id(agent_name),
        resolution,
        config,
        timing_scope=timing_scope,
    )


async def add_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    metadata: dict | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Add a memory for an agent."""
    if use_disabled_memory_backend(config, agent_name=agent_name):
        return
    if use_file_memory_backend(config, agent_name=agent_name):
        add_file_agent_memory(
            content,
            agent_name,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        return
    await add_mem0_agent_memory(
        content,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        metadata=metadata,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )


def append_agent_daily_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    preserve_resolved_storage_path: bool = False,
) -> MemoryResult:
    """Append one memory entry to today's per-agent daily memory file."""
    return append_agent_daily_file_memory(
        content,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
        execution_identity=execution_identity,
    )


@timed("system_prompt_assembly.memory_search")
async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int = 3,
    execution_identity: ToolExecutionIdentity | None = None,
    timing_scope: str | None = None,
) -> list[MemoryResult]:
    """Search agent memories including team memories."""
    if use_disabled_memory_backend(config, agent_name=agent_name):
        return []
    if use_file_memory_backend(config, agent_name=agent_name):
        return _search_file_backend_memories(
            query,
            agent_name,
            storage_path,
            config,
            runtime_paths,
            limit=limit,
            execution_identity=execution_identity,
            timing_scope=timing_scope,
        )
    return await _search_mem0_backend_memories(
        query,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        limit=limit,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int = 100,
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    preserve_resolved_storage_path: bool = False,
) -> list[MemoryResult]:
    """List all memories for an agent."""
    if use_disabled_memory_backend(config, agent_name=agent_name):
        return []
    if use_file_memory_backend(config, agent_name=agent_name):
        return list_file_agent_memories(
            agent_name,
            storage_path,
            config,
            runtime_paths,
            limit=limit,
            preserve_resolved_storage_path=preserve_resolved_storage_path,
            execution_identity=execution_identity,
        )
    return await list_mem0_agent_memories(
        agent_name,
        storage_path,
        config,
        runtime_paths,
        limit=limit,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )


async def get_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> MemoryResult | None:
    """Get a single memory by ID."""
    if caller_uses_disabled_memory_backend(config, caller_context):
        return None
    if caller_uses_file_memory_backend(config, caller_context):
        return get_file_agent_memory(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
    return await get_mem0_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )


async def update_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Update a single memory by ID."""
    if caller_uses_disabled_memory_backend(config, caller_context):
        return
    if caller_uses_file_memory_backend(config, caller_context):
        update_file_agent_memory(
            memory_id,
            content,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        return
    await update_mem0_agent_memory(
        memory_id,
        content,
        caller_context,
        storage_path,
        config,
        runtime_paths,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )


async def delete_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Delete a single memory by ID."""
    if caller_uses_disabled_memory_backend(config, caller_context):
        return
    if caller_uses_file_memory_backend(config, caller_context):
        delete_file_agent_memory(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        return
    await delete_mem0_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )


@timed("system_prompt_assembly.memory_enhancement")
async def build_memory_prompt_parts(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    timing_scope: str | None = None,
) -> MemoryPromptParts:
    """Split stable entrypoint context from turn-local searched memories."""
    logger.debug("Building enhanced prompt", agent=agent_name)
    if use_disabled_memory_backend(config, agent_name=agent_name):
        return MemoryPromptParts()

    use_file_backend = use_file_memory_backend(config, agent_name=agent_name)
    agent_memories = await search_agent_memories(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )
    if agent_memories:
        logger.debug("Agent memories added", count=len(agent_memories))

    session_preamble = ""
    context_type = "agent"
    if use_file_backend:
        agent_entrypoint = _load_agent_file_entrypoint_context(
            agent_name,
            storage_path,
            config,
            runtime_paths,
            execution_identity,
            timing_scope,
        )
        if agent_entrypoint:
            session_preamble = f"[File memory entrypoint (agent)]\n{agent_entrypoint}"
        context_type = "agent file"

    turn_context = _format_memories_as_context(agent_memories, context_type) if agent_memories else ""
    return MemoryPromptParts(
        session_preamble=session_preamble,
        turn_context=turn_context,
    )


async def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    timing_scope: str | None = None,
) -> str:
    """Compatibility wrapper that preserves the legacy monolithic prompt shape."""
    prompt_parts = await build_memory_prompt_parts(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )
    prompt_chunks = [chunk for chunk in (prompt_parts.session_preamble, prompt_parts.turn_context, prompt) if chunk]
    return "\n\n".join(prompt_chunks)


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    user_id: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Store conversation in memory for future recall."""
    if not prompt:
        return

    if isinstance(agent_name, str):
        if use_disabled_memory_backend(config, agent_name=agent_name):
            return
    elif team_uses_disabled_memory_backend(config, agent_name):
        return

    use_file_backend = (
        use_file_memory_backend(config, agent_name=agent_name)
        if isinstance(agent_name, str)
        else team_uses_file_memory_backend(config, agent_name)
    )
    if use_file_backend:
        store_file_conversation_memory(
            prompt,
            agent_name,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        return

    messages = build_memory_messages(prompt, thread_history, user_id)
    if not messages:
        return
    await store_mem0_conversation_memory(
        messages,
        agent_name,
        storage_path,
        session_id,
        config,
        runtime_paths,
        replica_key=new_memory_id() if isinstance(agent_name, list) else None,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )
