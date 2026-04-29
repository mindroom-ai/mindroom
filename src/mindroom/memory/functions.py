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
    team_members_by_memory_backend,
    team_members_from_scope_user_id,
    use_disabled_memory_backend,
    use_file_memory_backend,
)
from ._prompting import (
    _format_memories_as_context,
    build_memory_messages,
)
from ._shared import MEM0_REPLICA_KEY, MemoryNotFoundError, MemoryResult, new_memory_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._shared import ScopedMemoryCrud

logger = get_logger(__name__)


def _has_mixed_team_memory_backends(members_by_backend: dict[str, list[str]]) -> bool:
    return bool(members_by_backend["file"] and members_by_backend["mem0"])


def _requires_partitioned_team_memory_backend(members_by_backend: dict[str, list[str]]) -> bool:
    return bool(members_by_backend["none"] or _has_mixed_team_memory_backends(members_by_backend))


def _mem0_replica_key(memory: MemoryResult | None) -> str | None:
    if memory is None:
        return None
    metadata = memory.get("metadata")
    if not isinstance(metadata, dict):
        return None
    replica_key = metadata.get(MEM0_REPLICA_KEY)
    return replica_key if isinstance(replica_key, str) and replica_key else None


@dataclass(frozen=True)
class _MixedTeamMemoryTargetIds:
    file: str | None
    mem0: str | None


@dataclass(frozen=True)
class _PartitionedTeamMemoryContext:
    team_members: list[str]
    members_by_backend: dict[str, list[str]]


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


async def _get_mixed_team_agent_memory(
    memory_id: str,
    caller_context: list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    file_member_names: list[str],
    mem0_member_names: list[str],
) -> MemoryResult | None:
    if file_member_names:
        file_result = get_file_agent_memory(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            target_agent_names=file_member_names,
        )
        if file_result is not None:
            return file_result
    if not mem0_member_names:
        return None
    return await get_mem0_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
        target_agent_names=mem0_member_names,
    )


async def _resolve_mixed_team_memory_target_ids(
    memory_id: str,
    caller_context: list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    file_member_names: list[str],
    mem0_member_names: list[str],
) -> _MixedTeamMemoryTargetIds:
    file_memory_id: str | None = None
    mem0_memory_id: str | None = None
    file_result: MemoryResult | None = None
    mem0_result: MemoryResult | None = None

    if file_member_names:
        file_memory_id = memory_id
        file_result = get_file_agent_memory(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            target_agent_names=file_member_names,
        )
    if mem0_member_names:
        mem0_memory_id = memory_id
        mem0_result = await get_mem0_agent_memory(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            create_memory=_create_memory_factory(runtime_paths),
            execution_identity=execution_identity,
            target_agent_names=mem0_member_names,
        )

    if file_result is None:
        if (replica_key := _mem0_replica_key(mem0_result)) is not None:
            file_memory_id = replica_key
            file_result = get_file_agent_memory(
                file_memory_id,
                caller_context,
                storage_path,
                config,
                runtime_paths,
                execution_identity=execution_identity,
                target_agent_names=file_member_names,
            )
        else:
            file_memory_id = None
    if mem0_member_names and mem0_result is None:
        mem0_memory_id = file_memory_id
        if mem0_memory_id is not None:
            mem0_result = await get_mem0_agent_memory(
                mem0_memory_id,
                caller_context,
                storage_path,
                config,
                runtime_paths,
                create_memory=_create_memory_factory(runtime_paths),
                execution_identity=execution_identity,
                target_agent_names=mem0_member_names,
            )
        if mem0_result is None:
            mem0_memory_id = None

    return _MixedTeamMemoryTargetIds(
        file=file_memory_id if file_result is not None else None,
        mem0=mem0_memory_id if mem0_result is not None else None,
    )


async def _partitioned_team_memory_context_for_single_caller(
    memory_id: str,
    caller_context: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> _PartitionedTeamMemoryContext | None:
    memory = await get_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    if memory is None:
        return None
    user_id = memory.get("user_id")
    if not isinstance(user_id, str):
        return None
    team_members = team_members_from_scope_user_id(user_id, config)
    if team_members is None:
        return None
    members_by_backend = team_members_by_memory_backend(config, team_members)
    if not _requires_partitioned_team_memory_backend(members_by_backend):
        return None
    return _PartitionedTeamMemoryContext(
        team_members=team_members,
        members_by_backend=members_by_backend,
    )


async def _update_partitioned_team_memory(
    memory_id: str,
    content: str,
    team_members: list[str],
    members_by_backend: dict[str, list[str]],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    target_ids = await _resolve_mixed_team_memory_target_ids(
        memory_id,
        team_members,
        storage_path,
        config,
        runtime_paths,
        execution_identity,
        file_member_names=members_by_backend["file"],
        mem0_member_names=members_by_backend["mem0"],
    )
    if target_ids.file is not None:
        update_file_agent_memory(
            target_ids.file,
            content,
            team_members,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            target_agent_names=members_by_backend["file"],
        )
    if target_ids.mem0 is not None:
        await update_mem0_agent_memory(
            target_ids.mem0,
            content,
            team_members,
            storage_path,
            config,
            runtime_paths,
            create_memory=_create_memory_factory(runtime_paths),
            execution_identity=execution_identity,
            target_agent_names=members_by_backend["mem0"],
        )
    if target_ids.file is None and target_ids.mem0 is None:
        raise MemoryNotFoundError(memory_id)


async def _delete_partitioned_team_memory(
    memory_id: str,
    team_members: list[str],
    members_by_backend: dict[str, list[str]],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    target_ids = await _resolve_mixed_team_memory_target_ids(
        memory_id,
        team_members,
        storage_path,
        config,
        runtime_paths,
        execution_identity,
        file_member_names=members_by_backend["file"],
        mem0_member_names=members_by_backend["mem0"],
    )
    if target_ids.file is not None:
        delete_file_agent_memory(
            target_ids.file,
            team_members,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            target_agent_names=members_by_backend["file"],
        )
    if target_ids.mem0 is not None:
        await delete_mem0_agent_memory(
            target_ids.mem0,
            team_members,
            storage_path,
            config,
            runtime_paths,
            create_memory=_create_memory_factory(runtime_paths),
            execution_identity=execution_identity,
            target_agent_names=members_by_backend["mem0"],
        )
    if target_ids.file is None and target_ids.mem0 is None:
        raise MemoryNotFoundError(memory_id)


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
    if isinstance(caller_context, list):
        members_by_backend = team_members_by_memory_backend(config, caller_context)
        if _requires_partitioned_team_memory_backend(members_by_backend):
            return await _get_mixed_team_agent_memory(
                memory_id,
                caller_context,
                storage_path,
                config,
                runtime_paths,
                execution_identity,
                file_member_names=members_by_backend["file"],
                mem0_member_names=members_by_backend["mem0"],
            )

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
    if isinstance(caller_context, list):
        members_by_backend = team_members_by_memory_backend(config, caller_context)
        if _requires_partitioned_team_memory_backend(members_by_backend):
            await _update_partitioned_team_memory(
                memory_id,
                content,
                caller_context,
                members_by_backend,
                storage_path,
                config,
                runtime_paths,
                execution_identity,
            )
            return

    else:
        partitioned_context = await _partitioned_team_memory_context_for_single_caller(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity,
        )
        if partitioned_context is not None:
            await _update_partitioned_team_memory(
                memory_id,
                content,
                partitioned_context.team_members,
                partitioned_context.members_by_backend,
                storage_path,
                config,
                runtime_paths,
                execution_identity,
            )
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
    if isinstance(caller_context, list):
        members_by_backend = team_members_by_memory_backend(config, caller_context)
        if _requires_partitioned_team_memory_backend(members_by_backend):
            await _delete_partitioned_team_memory(
                memory_id,
                caller_context,
                members_by_backend,
                storage_path,
                config,
                runtime_paths,
                execution_identity,
            )
            return

    else:
        partitioned_context = await _partitioned_team_memory_context_for_single_caller(
            memory_id,
            caller_context,
            storage_path,
            config,
            runtime_paths,
            execution_identity,
        )
        if partitioned_context is not None:
            await _delete_partitioned_team_memory(
                memory_id,
                partitioned_context.team_members,
                partitioned_context.members_by_backend,
                storage_path,
                config,
                runtime_paths,
                execution_identity,
            )
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


async def _store_team_conversation_memory(
    prompt: str,
    agent_name: list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user_id: str | None,
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    members_by_backend = team_members_by_memory_backend(config, agent_name)
    file_member_names = members_by_backend["file"]
    mem0_member_names = members_by_backend["mem0"]
    team_replica_key = new_memory_id() if _has_mixed_team_memory_backends(members_by_backend) else None
    if file_member_names:
        store_file_conversation_memory(
            prompt,
            agent_name,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            target_agent_names=None if file_member_names == agent_name else file_member_names,
            memory_id=team_replica_key,
        )
    if not mem0_member_names:
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
        replica_key=team_replica_key or new_memory_id(),
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
        target_agent_names=None if mem0_member_names == agent_name else mem0_member_names,
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

    if isinstance(agent_name, list):
        await _store_team_conversation_memory(
            prompt,
            agent_name,
            storage_path,
            session_id,
            config,
            runtime_paths,
            thread_history,
            user_id,
            execution_identity,
        )
        return

    if use_disabled_memory_backend(config, agent_name=agent_name):
        return
    if use_file_memory_backend(config, agent_name=agent_name):
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
        replica_key=None,
        create_memory=_create_memory_factory(runtime_paths),
        execution_identity=execution_identity,
    )
