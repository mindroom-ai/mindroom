"""Public memory API and orchestration."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.prompt_templates import render_prompt_template
from mindroom.timing import timed

from ._backend import resolve_memory_backend
from ._entrypoint_snapshots import stable_entrypoint_preamble
from ._file_backend import append_agent_daily_file_memory
from ._prompting import format_memories_as_context
from ._shared import MemoryEntrypointContext, MemorySearchOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._backend import ResolvedMemoryBackend
    from ._entrypoint_snapshots import ConversationRef
    from ._shared import MemoryResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class MemoryPromptParts:
    """Stable and turn-local prompt fragments used by the AI layer."""

    session_preamble: str = ""
    transient_turn_context: str = ""


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
) -> MemoryResult:
    """Append one memory entry to today's per-agent daily memory file."""
    return append_agent_daily_file_memory(
        content,
        agent_name,
        storage_path,
        config,
        runtime_paths,
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
    editable: bool = False,
) -> MemorySearchOutcome:
    """Search agent memories including team memories; ``editable`` returns only IDs update and delete accept."""
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return MemorySearchOutcome(results=[])
    return await backend.search(
        query,
        agent_name,
        storage_path,
        config,
        limit=limit,
        execution_identity=execution_identity,
        editable=editable,
    )


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int = 100,
    execution_identity: ToolExecutionIdentity | None = None,
) -> list[MemoryResult]:
    """List all memories for an agent."""
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return []
    return await backend.list_all(
        agent_name,
        storage_path,
        config,
        limit=limit,
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


def _render_entrypoint_body(entrypoint: MemoryEntrypointContext, config: Config, *, kept_copy: bool) -> str:
    """Frame the preloaded memory entrypoint as already-inlined context, without its truncation marker.

    A ``kept_copy`` body, one a conversation may keep while entries are added, says newer entries may be missing.
    """
    header = render_prompt_template(
        config.get_prompt("FILE_MEMORY_ENTRYPOINT_HEADER_TEMPLATE"),
        memory_path=entrypoint.source_path,
    )
    kept_copy_notice = config.get_prompt("FILE_MEMORY_ENTRYPOINT_KEPT_COPY_NOTICE") if kept_copy else ""
    return "\n".join(part for part in (header, kept_copy_notice, entrypoint.text) if part)


def _render_truncation_notice(entrypoint: MemoryEntrypointContext, config: Config) -> str:
    """Report what the caps withheld, even when the preloaded head is blank, so the cap never withholds lines silently."""
    if not entrypoint.omitted_lines:
        return ""
    return render_prompt_template(
        config.get_prompt("FILE_MEMORY_ENTRYPOINT_TRUNCATION_TEMPLATE"),
        included_lines=entrypoint.included_lines,
        total_lines=entrypoint.total_lines,
        max_entrypoint_lines=config.memory.file.max_entrypoint_lines,
        max_entrypoint_tokens=config.memory.file.max_entrypoint_tokens,
        memory_path=entrypoint.source_path,
    )


async def _entrypoint_preamble(
    backend: ResolvedMemoryBackend,
    agent_name: str,
    storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    conversation: ConversationRef | None,
) -> str:
    """Render the agent's `MEMORY.md` preamble, or the copy ``conversation`` keeps of it."""
    # The file backend reads the scoped MEMORY.md from disk; keep it off the
    # event loop (#1260).
    entrypoint = await asyncio.to_thread(
        backend.load_entrypoint_context,
        agent_name,
        storage_path,
        config,
        execution_identity=execution_identity,
    )
    body = (
        _render_entrypoint_body(entrypoint, config, kept_copy=conversation is not None)
        if entrypoint.text or entrypoint.omitted_lines
        else ""
    )
    preamble = "\n".join(part for part in (body, _render_truncation_notice(entrypoint, config)) if part)
    if conversation is None or entrypoint.source_path is None:
        return preamble
    return stable_entrypoint_preamble(
        agent_name,
        conversation,
        entrypoint.source_path,
        body,
        preamble,
        now=time.monotonic(),
    )


async def conversation_entrypoint_preamble(
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    conversation: ConversationRef,
) -> str:
    """Return the `MEMORY.md` preamble for ``conversation``, loading the file again when its history was compacted."""
    backend = resolve_memory_backend(agent_name, config, runtime_paths)
    if backend is None:
        return ""
    return await _entrypoint_preamble(backend, agent_name, storage_path, config, execution_identity, conversation)


@timed("system_prompt_assembly.memory_enhancement")
async def build_memory_prompt_parts(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    conversation: ConversationRef | None = None,
) -> MemoryPromptParts:
    """Split stable entrypoint context from turn-local searched memories.

    A turn in ``conversation`` keeps the entrypoint that conversation last sent while its inlined copy only gains lines
    and its prompt cache may be warm.
    """
    logger.debug("Building enhanced prompt", agent=agent_name)
    if (backend := resolve_memory_backend(agent_name, config, runtime_paths)) is None:
        return MemoryPromptParts()

    search_outcome = await search_agent_memories(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    agent_memories = search_outcome.results
    if agent_memories:
        logger.debug("Agent memories added", count=len(agent_memories))

    session_preamble = await _entrypoint_preamble(
        backend,
        agent_name,
        storage_path,
        config,
        execution_identity,
        conversation,
    )

    # The automatic per-turn path must not silently drop the degradation
    # signal: a broken embedder would otherwise look like an agent with no
    # relevant memories, the original ISSUE-237 failure shape.
    degradation_notice = ""
    if search_outcome.degraded_reason is not None:
        degradation_notice = (
            f"Semantic memory search is unavailable this turn ({search_outcome.degraded_reason}); "
            "stored memories may be missing or keyword-only. Do not claim to have checked stored memories."
        )

    memory_context = (
        format_memories_as_context(
            agent_memories,
            backend.context_label,
            prompt_template=config.get_prompt("MEMORY_CONTEXT_PROMPT_TEMPLATE"),
        )
        if agent_memories
        else ""
    )
    transient_turn_context = "\n\n".join(part for part in (degradation_notice, memory_context) if part)
    return MemoryPromptParts(
        session_preamble=session_preamble,
        transient_turn_context=transient_turn_context,
    )


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
