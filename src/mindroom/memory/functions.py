"""Public memory API and orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.timing import timed

from ._backend import resolve_memory_backend
from ._file_backend import append_agent_daily_file_memory
from ._prompting import format_memories_as_context

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._shared import MemoryResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class MemoryPromptParts:
    """Stable and turn-local prompt fragments used by the AI layer."""

    session_preamble: str = ""
    turn_context: str = ""


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
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return
    await backend.add(
        content,
        agent_name,
        storage_path,
        config,
        metadata=metadata,
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
) -> list[MemoryResult]:
    """Search agent memories including team memories."""
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return []
    return await backend.search(
        query,
        agent_name,
        storage_path,
        config,
        limit=limit,
        execution_identity=execution_identity,
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
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return []
    return await backend.list_all(
        agent_name,
        storage_path,
        config,
        limit=limit,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
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
    if (backend := resolve_memory_backend(caller_context, config, runtime_paths)) is None:
        return None
    return await backend.get(
        memory_id,
        caller_context,
        storage_path,
        config,
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
    if (backend := resolve_memory_backend(caller_context, config, runtime_paths)) is None:
        return
    await backend.update(
        memory_id,
        content,
        caller_context,
        storage_path,
        config,
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
    if (backend := resolve_memory_backend(caller_context, config, runtime_paths)) is None:
        return
    await backend.delete(
        memory_id,
        caller_context,
        storage_path,
        config,
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
) -> MemoryPromptParts:
    """Split stable entrypoint context from turn-local searched memories."""
    logger.debug("Building enhanced prompt", agent=agent_name)
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return MemoryPromptParts()

    agent_memories = await search_agent_memories(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    if agent_memories:
        logger.debug("Agent memories added", count=len(agent_memories))

    session_preamble = ""
    # The file backend reads the scoped MEMORY.md from disk; keep it off the
    # event loop (#1260).
    agent_entrypoint = await asyncio.to_thread(
        backend.load_entrypoint_context,
        agent_name,
        storage_path,
        config,
        execution_identity=execution_identity,
    )
    if agent_entrypoint:
        session_preamble = f"{config.get_prompt('FILE_MEMORY_ENTRYPOINT_HEADER')}\n{agent_entrypoint}"

    turn_context = (
        format_memories_as_context(
            agent_memories,
            backend.context_label,
            prompt_template=config.get_prompt("MEMORY_CONTEXT_PROMPT_TEMPLATE"),
        )
        if agent_memories
        else ""
    )
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
) -> str:
    """Compatibility wrapper that preserves the legacy monolithic prompt shape."""
    prompt_parts = await build_memory_prompt_parts(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
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
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return
    await backend.store_conversation(
        prompt,
        agent_name,
        storage_path,
        session_id,
        config,
        thread_history=thread_history,
        user_id=user_id,
        execution_identity=execution_identity,
    )
