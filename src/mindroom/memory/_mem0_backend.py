"""Mem0-backed memory implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger

from ._policy import (
    agent_scope_user_id,
    build_team_user_id,
    effective_storage_paths_for_context,
    get_allowed_memory_user_ids,
    get_team_ids_for_agent,
    mutation_target_storage_paths,
    resolve_context_storage_path,
    room_scope_user_id,
)
from ._shared import MEM0_REPLICA_KEY, MemoryNotFoundError, MemoryResult, ScopedMemoryCrud, ScopedMemoryWriter

if TYPE_CHECKING:
    from mindroom.config.main import Config

_MemoryFactory = Callable[[Path, "Config"], Awaitable[ScopedMemoryCrud]]

logger = get_logger(__name__)


def _mem0_results(payload: object) -> list[MemoryResult]:
    if isinstance(payload, dict):
        payload_dict = cast("dict[str, object]", payload)
        results = payload_dict.get("results")
        if isinstance(results, list):
            return cast("list[MemoryResult]", results)
    return []


async def _get_scoped_memory_by_id(
    memory: ScopedMemoryCrud,
    memory_id: str,
    caller_context: str | list[str],
    config: Config,
) -> MemoryResult | None:
    result = await memory.get(memory_id)
    if not isinstance(result, dict):
        allowed_user_ids = get_allowed_memory_user_ids(caller_context, config)
        for scope_user_id in sorted(allowed_user_ids):
            for entry in _mem0_results(await memory.get_all(user_id=scope_user_id, limit=1000)):
                if not isinstance(entry, dict):
                    continue
                metadata = entry.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                if metadata.get(MEM0_REPLICA_KEY) == memory_id:
                    return cast("MemoryResult", entry)
        return None

    allowed_user_ids = get_allowed_memory_user_ids(caller_context, config)
    memory_user_id = result.get("user_id")
    if memory_user_id not in allowed_user_ids:
        logger.warning(
            "Memory access denied",
            memory_id=memory_id,
            memory_user_id=memory_user_id,
            allowed_user_ids=sorted(allowed_user_ids),
        )
        return None

    return cast("MemoryResult", result)


def _mem0_replica_key(result: MemoryResult) -> str | None:
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return None
    replica_key = metadata.get(MEM0_REPLICA_KEY)
    return replica_key if isinstance(replica_key, str) and replica_key else None


async def _find_mem0_replica_memory_ids(
    *,
    memory: ScopedMemoryCrud,
    scope_user_id: str,
    anchor_result: MemoryResult,
) -> list[str]:
    replica_key = _mem0_replica_key(anchor_result)

    matches: list[str] = []
    for entry in _mem0_results(await memory.get_all(user_id=scope_user_id, limit=1000)):
        if not isinstance(entry, dict):
            continue
        if entry.get("user_id") != scope_user_id:
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue

        if replica_key is not None:
            metadata = entry.get("metadata")
            if isinstance(metadata, dict) and metadata.get(MEM0_REPLICA_KEY) == replica_key:
                matches.append(entry_id)
            continue

        if entry.get("memory") == anchor_result.get("memory") and entry.get("metadata") == anchor_result.get(
            "metadata",
        ):
            matches.append(entry_id)

    if replica_key is None and len(matches) != 1:
        return []
    return matches


async def _find_mem0_anchor_memory_result(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    *,
    create_memory: _MemoryFactory,
) -> MemoryResult | None:
    for target_storage_path in effective_storage_paths_for_context(caller_context, storage_path, config):
        memory = await create_memory(target_storage_path, config)
        if result := await _get_scoped_memory_by_id(memory, memory_id, caller_context, config):
            return result
    return None


async def _mem0_mutation_target_ids(
    memory: ScopedMemoryCrud,
    memory_id: str,
    scope_user_id: str,
    caller_context: str | list[str],
    anchor_result: MemoryResult,
    config: Config,
) -> list[str]:
    direct_match = await _get_scoped_memory_by_id(memory, memory_id, caller_context, config)
    if direct_match is not None and isinstance(direct_match.get("id"), str):
        return [direct_match["id"]]
    return await _find_mem0_replica_memory_ids(
        memory=memory,
        scope_user_id=scope_user_id,
        anchor_result=anchor_result,
    )


async def _mutate_mem0_memory_targets(
    *,
    memory_id: str,
    content: str | None,
    operation: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    anchor_result: MemoryResult,
    create_memory: _MemoryFactory,
) -> int:
    mutated_targets = 0
    scope_user_id = anchor_result["user_id"]
    for target_storage_path in mutation_target_storage_paths(scope_user_id, caller_context, storage_path, config):
        memory = await create_memory(target_storage_path, config)
        target_ids = await _mem0_mutation_target_ids(
            memory,
            memory_id,
            scope_user_id,
            caller_context,
            anchor_result,
            config,
        )
        for target_id in dict.fromkeys(target_ids):
            if operation == "update":
                await memory.update(target_id, cast("str", content))
            else:
                await memory.delete(target_id)
            mutated_targets += 1
    return mutated_targets


async def _add_mem0_scope_messages(
    *,
    memory: ScopedMemoryWriter,
    messages: list[dict],
    user_id: str,
    metadata: dict[str, object],
    failure_log: str,
    failure_context: dict[str, object],
) -> None:
    try:
        await memory.add(messages, user_id=user_id, metadata=metadata)
    except Exception as error:
        logger.exception(failure_log, error=str(error), **failure_context)


async def add_mem0_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    metadata: dict | None,
    create_memory: _MemoryFactory,
) -> None:
    """Add one mem0 memory for an agent scope."""
    resolved_storage_path = resolve_context_storage_path(storage_path, config, agent_name=agent_name)
    memory = await create_memory(resolved_storage_path, config)
    metadata = dict(metadata or {})
    metadata["agent"] = agent_name
    messages = [{"role": "user", "content": content}]
    try:
        await memory.add(messages, user_id=agent_scope_user_id(agent_name), metadata=metadata)
        logger.info("Memory added", agent=agent_name)
    except Exception:
        logger.exception("Failed to add memory", agent=agent_name)
        raise


async def search_mem0_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    limit: int,
    create_memory: _MemoryFactory,
) -> list[MemoryResult]:
    """Search mem0 memories visible to an agent."""
    resolved_storage_path = resolve_context_storage_path(storage_path, config, agent_name=agent_name)
    memory = await create_memory(resolved_storage_path, config)

    results = _mem0_results(await memory.search(query, user_id=agent_scope_user_id(agent_name), limit=limit))

    for team_id in get_team_ids_for_agent(agent_name, config):
        team_memories = _mem0_results(await memory.search(query, user_id=team_id, limit=limit))
        existing_memories = {result.get("memory", "") for result in results}
        for memory_result in team_memories:
            if memory_result.get("memory", "") not in existing_memories:
                results.append(memory_result)
        logger.debug("Team memories found", team_id=team_id, count=len(team_memories))

    logger.debug("Total memories found", count=len(results), agent=agent_name)
    return results[:limit]


async def list_mem0_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    limit: int,
    create_memory: _MemoryFactory,
) -> list[MemoryResult]:
    """List mem0 memories stored for an agent."""
    resolved_storage_path = resolve_context_storage_path(storage_path, config, agent_name=agent_name)
    result = await create_memory(resolved_storage_path, config)
    return _mem0_results(await result.get_all(user_id=agent_scope_user_id(agent_name), limit=limit))


async def get_mem0_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    *,
    create_memory: _MemoryFactory,
) -> MemoryResult | None:
    """Return one mem0 memory visible to the caller."""
    for target_storage_path in effective_storage_paths_for_context(caller_context, storage_path, config):
        memory = await create_memory(target_storage_path, config)
        result = await _get_scoped_memory_by_id(memory, memory_id, caller_context, config)
        if result is not None:
            return result
    return None


async def update_mem0_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    *,
    create_memory: _MemoryFactory,
) -> None:
    """Update one mem0 memory across its replica targets."""
    if (
        anchor_result := await _find_mem0_anchor_memory_result(
            memory_id,
            caller_context,
            storage_path,
            config,
            create_memory=create_memory,
        )
    ) is None:
        raise MemoryNotFoundError(memory_id)

    updated_targets = await _mutate_mem0_memory_targets(
        memory_id=memory_id,
        content=content,
        operation="update",
        caller_context=caller_context,
        storage_path=storage_path,
        config=config,
        anchor_result=anchor_result,
        create_memory=create_memory,
    )
    if updated_targets > 0:
        logger.info("Memory updated", memory_id=memory_id, storage_targets=updated_targets)
        return
    raise MemoryNotFoundError(memory_id)


async def delete_mem0_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    *,
    create_memory: _MemoryFactory,
) -> None:
    """Delete one mem0 memory across its replica targets."""
    if (
        anchor_result := await _find_mem0_anchor_memory_result(
            memory_id,
            caller_context,
            storage_path,
            config,
            create_memory=create_memory,
        )
    ) is None:
        raise MemoryNotFoundError(memory_id)

    deleted_targets = await _mutate_mem0_memory_targets(
        memory_id=memory_id,
        content=None,
        operation="delete",
        caller_context=caller_context,
        storage_path=storage_path,
        config=config,
        anchor_result=anchor_result,
        create_memory=create_memory,
    )
    if deleted_targets > 0:
        logger.info("Memory deleted", memory_id=memory_id, storage_targets=deleted_targets)
        return
    raise MemoryNotFoundError(memory_id)


async def add_mem0_room_memory(
    content: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    *,
    agent_name: str | None,
    metadata: dict | None,
    create_memory: _MemoryFactory,
) -> None:
    """Add one mem0 memory for a room scope."""
    resolved_storage_path = resolve_context_storage_path(storage_path, config, agent_name=agent_name)
    memory = await create_memory(resolved_storage_path, config)

    metadata = dict(metadata or {})
    metadata["room_id"] = room_id
    if agent_name:
        metadata["contributed_by"] = agent_name

    messages = [{"role": "user", "content": content}]
    await memory.add(messages, user_id=room_scope_user_id(room_id), metadata=metadata)
    logger.debug("Room memory added", room_id=room_id)


async def search_mem0_room_memories(
    query: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    *,
    agent_name: str | None,
    limit: int,
    create_memory: _MemoryFactory,
) -> list[MemoryResult]:
    """Search mem0 memories stored for a room scope."""
    resolved_storage_path = resolve_context_storage_path(storage_path, config, agent_name=agent_name)
    memory = await create_memory(resolved_storage_path, config)
    results = _mem0_results(await memory.search(query, user_id=room_scope_user_id(room_id), limit=limit))
    logger.debug("Room memories found", count=len(results), room_id=room_id)
    return results


async def store_mem0_conversation_memory(
    messages: list[dict],
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    room_id: str | None,
    *,
    replica_key: str | None,
    create_memory: _MemoryFactory,
) -> None:
    """Persist conversation messages to mem0-backed memory scopes."""
    target_storage_paths = effective_storage_paths_for_context(agent_name, storage_path, config)

    if isinstance(agent_name, list):
        scope_user_id = build_team_user_id(agent_name)
        metadata = {
            "type": "conversation",
            "session_id": session_id,
            "is_team": True,
            "team_members": agent_name,
        }
        if replica_key is not None:
            metadata[MEM0_REPLICA_KEY] = replica_key
        failure_log = "Failed to add team memory"
        failure_context: dict[str, object] = {"team_id": scope_user_id}
    else:
        scope_user_id = agent_scope_user_id(agent_name)
        metadata = {
            "type": "conversation",
            "session_id": session_id,
            "agent": agent_name,
        }
        failure_log = "Failed to add memory"
        failure_context = {"agent": agent_name}

    room_scope_id: str | None = None
    room_metadata: dict[str, object] | None = None
    if room_id:
        contributed_by = agent_name if isinstance(agent_name, str) else f"team:{','.join(agent_name)}"
        room_metadata = {
            "type": "conversation",
            "session_id": session_id,
            "room_id": room_id,
            "contributed_by": contributed_by,
        }
        room_scope_id = room_scope_user_id(room_id)

    for target_storage_path in target_storage_paths:
        memory = await create_memory(target_storage_path, config)
        await _add_mem0_scope_messages(
            memory=memory,
            messages=messages,
            user_id=scope_user_id,
            metadata=metadata,
            failure_log=failure_log,
            failure_context=failure_context,
        )
        if room_scope_id is not None and room_metadata is not None:
            await _add_mem0_scope_messages(
                memory=memory,
                messages=messages,
                user_id=room_scope_id,
                metadata=room_metadata,
                failure_log="Failed to add room memory",
                failure_context={"room_id": room_id},
            )

    if isinstance(agent_name, list):
        logger.info(
            "Team memory added",
            team_id=scope_user_id,
            members=agent_name,
            storage_targets=len(target_storage_paths),
        )
    else:
        logger.info("Memory added", agent=agent_name)

    if room_id:
        logger.debug("Room memory added", room_id=room_id, storage_targets=len(target_storage_paths))
