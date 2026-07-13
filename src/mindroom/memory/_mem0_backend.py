"""Mem0-backed memory implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeVar, cast, runtime_checkable

from mindroom.embedder_health import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
)
from mindroom.embedding_errors import EmbedderRequestError, classified_embedder_error
from mindroom.logging_config import get_logger
from mindroom.timing import timed

from ._policy import (
    agent_scope_user_id,
    allowed_scope_storage_paths,
    build_team_user_id,
    effective_storage_paths_for_context,
    get_allowed_memory_user_ids,
    get_team_ids_for_agent,
    storage_paths_for_scope_user_id,
)
from ._prompting import build_memory_messages
from ._shared import (
    MEM0_REPLICA_KEY,
    MemoryNotFoundError,
    MemoryResult,
    MemorySearchOutcome,
    ScopedMemoryCrud,
    ScopedMemoryWriter,
    new_memory_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_MemoryFactory = Callable[..., Awaitable[ScopedMemoryCrud]]
_T = TypeVar("_T")

logger = get_logger(__name__)


@runtime_checkable
class _OperationTrackedEmbedder(Protocol):
    def begin_operation(self) -> None: ...

    def raise_for_operation_failure(self) -> None: ...


def _record_classified_embedder_failure(
    exc: BaseException,
    health_recorder: EmbedderHealthRecorder,
) -> str | None:
    detail = classified_embedder_error(exc)
    if detail is not None:
        health_recorder.record(detail)
    return detail


async def _run_mem0_embedder_mutation(
    operation: Callable[[], Awaitable[_T]],
    health_recorder: EmbedderHealthRecorder,
) -> _T:
    """Run a Mem0 write without allowing raw provider exceptions to escape."""
    try:
        result = await operation()
    except Exception as exc:
        detail = _record_classified_embedder_failure(exc, health_recorder)
        if detail is None:
            raise
        raise EmbedderRequestError(detail) from None
    health_recorder.record(None)
    return result


async def _strict_mem0_add(
    memory: ScopedMemoryWriter,
    messages: list[dict],
    *,
    user_id: str,
    metadata: dict[str, object],
) -> object:
    """Add through Mem0 and surface embedding failures it catches internally."""
    embedder = getattr(memory, "embedding_model", None)
    if isinstance(embedder, _OperationTrackedEmbedder):
        embedder.begin_operation()
    result = await memory.add(messages, user_id=user_id, metadata=metadata)
    if isinstance(embedder, _OperationTrackedEmbedder):
        embedder.raise_for_operation_failure()
    return result


def _mem0_results(payload: object) -> list[MemoryResult]:
    if isinstance(payload, dict):
        payload_dict = cast("dict[str, object]", payload)
        results = payload_dict.get("results")
        if isinstance(results, list):
            return cast("list[MemoryResult]", results)
    return []


def _scope_filter(scope_user_id: str) -> dict[str, object]:
    return {"user_id": scope_user_id}


def _primary_mem0_storage_path(
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path:
    """Return the canonical mem0 storage root for one agent in the active scope."""
    return effective_storage_paths_for_context(
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )[0]


@timed("system_prompt_assembly.memory_search.mem0.create_memory_instance")
async def _create_mem0_memory_instance(
    resolved_storage_path: Path,
    config: Config,
    create_memory: _MemoryFactory,
) -> ScopedMemoryCrud:
    return await create_memory(resolved_storage_path, config)


@timed("system_prompt_assembly.memory_search.mem0.agent_search")
async def _search_mem0_agent_scope(
    memory: ScopedMemoryCrud,
    query: str,
    agent_name: str,
    limit: int,
) -> list[MemoryResult]:
    return _mem0_results(
        await memory.search(
            query,
            filters=_scope_filter(agent_scope_user_id(agent_name)),
            top_k=limit,
        ),
    )


@timed("system_prompt_assembly.memory_search.mem0.team_search")
async def _search_mem0_team_scope(
    memory: ScopedMemoryCrud,
    query: str,
    team_id: str,
    limit: int,
) -> list[MemoryResult]:
    return _mem0_results(await memory.search(query, filters=_scope_filter(team_id), top_k=limit))


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
            for entry in _mem0_results(await memory.get_all(filters=_scope_filter(scope_user_id), top_k=1000)):
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
    for entry in _mem0_results(await memory.get_all(filters=_scope_filter(scope_user_id), top_k=1000)):
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
    runtime_paths: RuntimePaths,
    *,
    create_memory: _MemoryFactory,
    execution_identity: ToolExecutionIdentity | None = None,
) -> MemoryResult | None:
    for _scope_user_id, target_storage_path in allowed_scope_storage_paths(
        caller_context,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ):
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
    runtime_paths: RuntimePaths,
    anchor_result: MemoryResult,
    create_memory: _MemoryFactory,
    execution_identity: ToolExecutionIdentity | None = None,
) -> int:
    mutated_targets = 0
    scope_user_id = anchor_result["user_id"]
    for target_storage_path in storage_paths_for_scope_user_id(
        scope_user_id,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ):
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
    health_recorder: EmbedderHealthRecorder,
) -> bool:
    """Write one Mem0 replica and report whether it actually succeeded."""
    try:
        await _strict_mem0_add(memory, messages, user_id=user_id, metadata=metadata)
    except Exception as error:
        if (detail := _record_classified_embedder_failure(error, health_recorder)) is not None:
            logger.warning(failure_log, error=detail, **failure_context)
            return False
        logger.exception(failure_log, error_type=type(error).__name__, **failure_context)
        return False
    health_recorder.record(None)
    return True


@dataclass(frozen=True)
class Mem0MemoryBackend:
    """Mem0-backed adapter implementing the shared memory backend surface."""

    runtime_paths: RuntimePaths
    create_memory: _MemoryFactory
    context_label: ClassVar[str] = "agent"

    async def add(
        self,
        content: str,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        metadata: dict | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Add one mem0 memory for an agent scope."""
        resolved_storage_path = _primary_mem0_storage_path(
            agent_name,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )
        metadata = dict(metadata or {})
        metadata["agent"] = agent_name
        messages = [{"role": "user", "content": content}]
        health_recorder = capture_embedder_health_recorder()

        async def add_memory() -> None:
            memory = await self.create_memory(resolved_storage_path, config)
            await _strict_mem0_add(
                memory,
                messages,
                user_id=agent_scope_user_id(agent_name),
                metadata=metadata,
            )

        await _run_mem0_embedder_mutation(add_memory, health_recorder)
        logger.info("Memory added", agent=agent_name)

    @timed("system_prompt_assembly.memory_search.mem0_backend")
    async def search(
        self,
        query: str,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemorySearchOutcome:
        """Search mem0 memories visible to an agent.

        Mem0 builds its own internal embedder, so passive health recording in
        MindRoom's embedder never sees this traffic: the backend records the
        outcome itself. Mem0's search embeds the query without swallowing
        (verified against mem0ai 2.0.1), so a completed search proves the
        embedder round-tripped and a propagated provider error classifies
        into a degraded outcome instead of raising.
        """
        resolved_storage_path = _primary_mem0_storage_path(
            agent_name,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )
        health_recorder = capture_embedder_health_recorder()
        try:
            memory = await _create_mem0_memory_instance(
                resolved_storage_path,
                config,
                self.create_memory,
            )
            results = await _search_mem0_agent_scope(memory, query, agent_name, limit)
        except Exception as exc:
            degraded_reason = _record_classified_embedder_failure(exc, health_recorder)
            if degraded_reason is None:
                raise
            logger.warning("Mem0 memory search degraded by embedder failure", agent=agent_name, error=degraded_reason)
            return MemorySearchOutcome(results=[], degraded_reason=degraded_reason)

        existing_memories = {result.get("memory", "") for result in results}
        degraded_reason: str | None = None
        for team_id in get_team_ids_for_agent(agent_name, config):
            try:
                team_memories = await _search_mem0_team_scope(memory, query, team_id, limit)
            except Exception as exc:
                scope_failure = _record_classified_embedder_failure(exc, health_recorder)
                if scope_failure is None:
                    raise
                degraded_reason = degraded_reason or scope_failure
                logger.warning(
                    "Mem0 team memory search degraded by embedder failure",
                    agent=agent_name,
                    team_id=team_id,
                    error=scope_failure,
                )
                continue
            for memory_result in team_memories:
                if memory_result.get("memory", "") not in existing_memories:
                    results.append(memory_result)
                    existing_memories.add(memory_result.get("memory", ""))
            logger.debug("Team memories found", team_id=team_id, count=len(team_memories))

        logger.debug("Total memories found", count=len(results), agent=agent_name)
        if degraded_reason is None:
            health_recorder.record(None)
        return MemorySearchOutcome(results=results[:limit], degraded_reason=degraded_reason)

    async def list_all(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        limit: int,
        preserve_resolved_storage_path: bool = False,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> list[MemoryResult]:
        """List mem0 memories stored for an agent."""
        del preserve_resolved_storage_path  # Only meaningful for file-backed storage roots.
        resolved_storage_path = _primary_mem0_storage_path(
            agent_name,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )
        result = await self.create_memory(resolved_storage_path, config)
        return _mem0_results(await result.get_all(filters=_scope_filter(agent_scope_user_id(agent_name)), top_k=limit))

    async def get(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one mem0 memory visible to the caller."""
        return await _find_mem0_anchor_memory_result(
            memory_id,
            caller_context,
            storage_path,
            config,
            self.runtime_paths,
            create_memory=self.create_memory,
            execution_identity=execution_identity,
        )

    async def update(
        self,
        memory_id: str,
        content: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Update one mem0 memory across its replica targets."""
        health_recorder = capture_embedder_health_recorder()

        async def update_targets() -> int:
            if (
                anchor_result := await _find_mem0_anchor_memory_result(
                    memory_id,
                    caller_context,
                    storage_path,
                    config,
                    self.runtime_paths,
                    create_memory=self.create_memory,
                    execution_identity=execution_identity,
                )
            ) is None:
                raise MemoryNotFoundError(memory_id)
            return await _mutate_mem0_memory_targets(
                memory_id=memory_id,
                content=content,
                operation="update",
                caller_context=caller_context,
                storage_path=storage_path,
                config=config,
                runtime_paths=self.runtime_paths,
                anchor_result=anchor_result,
                create_memory=self.create_memory,
                execution_identity=execution_identity,
            )

        updated_targets = await _run_mem0_embedder_mutation(update_targets, health_recorder)
        if updated_targets > 0:
            logger.info("Memory updated", memory_id=memory_id, storage_targets=updated_targets)
            return
        raise MemoryNotFoundError(memory_id)

    async def delete(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one mem0 memory across its replica targets."""
        if (
            anchor_result := await _find_mem0_anchor_memory_result(
                memory_id,
                caller_context,
                storage_path,
                config,
                self.runtime_paths,
                create_memory=self.create_memory,
                execution_identity=execution_identity,
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
            runtime_paths=self.runtime_paths,
            anchor_result=anchor_result,
            create_memory=self.create_memory,
            execution_identity=execution_identity,
        )
        if deleted_targets > 0:
            logger.info("Memory deleted", memory_id=memory_id, storage_targets=deleted_targets)
            return
        raise MemoryNotFoundError(memory_id)

    async def store_conversation(
        self,
        prompt: str,
        agent_name: str | list[str],
        storage_path: Path,
        session_id: str,
        config: Config,
        *,
        thread_history: Sequence[ResolvedVisibleMessage] | None = None,
        user_id: str | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Persist conversation messages to mem0-backed memory scopes."""
        messages = build_memory_messages(prompt, thread_history, user_id)
        if not messages:
            return
        replica_key = new_memory_id() if isinstance(agent_name, list) else None

        target_storage_paths = effective_storage_paths_for_context(
            agent_name,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )

        metadata: dict[str, object]
        failure_context: dict[str, object]
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
            failure_context = {"team_id": scope_user_id}
        else:
            scope_user_id = agent_scope_user_id(agent_name)
            metadata = {
                "type": "conversation",
                "session_id": session_id,
                "agent": agent_name,
            }
            failure_log = "Failed to add memory"
            failure_context = {"agent": agent_name}

        stored_targets = 0
        for target_storage_path in target_storage_paths:
            health_recorder = capture_embedder_health_recorder()
            try:
                memory = await self.create_memory(target_storage_path, config)
            except Exception as exc:
                if (detail := _record_classified_embedder_failure(exc, health_recorder)) is None:
                    raise
                logger.warning(failure_log, error=detail, **failure_context)
                continue
            if await _add_mem0_scope_messages(
                memory=memory,
                messages=messages,
                user_id=scope_user_id,
                metadata=metadata,
                failure_log=failure_log,
                failure_context=failure_context,
                health_recorder=health_recorder,
            ):
                stored_targets += 1

        if stored_targets == 0:
            logger.warning(
                "Conversation memory was not stored in any target",
                requested_targets=len(target_storage_paths),
                **failure_context,
            )
            return

        if isinstance(agent_name, list):
            logger.info(
                "Team memory added",
                team_id=scope_user_id,
                members=agent_name,
                storage_targets=stored_targets,
                requested_targets=len(target_storage_paths),
            )
        else:
            logger.info(
                "Memory added",
                agent=agent_name,
                storage_targets=stored_targets,
                requested_targets=len(target_storage_paths),
            )

    def load_entrypoint_context(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> str:
        """Return no stable entrypoint context; mem0 has no curated `MEMORY.md`."""
        del agent_name, storage_path, config, execution_identity
        return ""
