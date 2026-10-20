"""Team memory adapter for teams whose members use different memory backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ._mem0_backend import mem0_replica_key
from ._shared import MemoryNotFoundError, new_memory_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._file_backend import FileMemoryBackend
    from ._mem0_backend import Mem0MemoryBackend
    from ._policy import TeamMemoryBackendMembers
    from ._shared import MemoryEntrypointContext, MemoryResult, MemorySearchOutcome


@dataclass(frozen=True)
class _TeamMemoryTargetIds:
    """IDs of one team memory in each backend, or None where that backend has no copy."""

    file: str | None
    mem0: str | None


def _agent_scope_error(agent_name: str) -> TypeError:
    return TypeError(f"Team memory backend cannot serve agent scope {agent_name}; resolve the agent's own backend")


@dataclass(frozen=True)
class PartitionedTeamMemoryBackend:
    """Team adapter that keeps each member's copy of team memory in that member's backend.

    File-backed members hold team memory in file team scope and Mem0-backed
    members in Mem0 team scope, under one shared replica key so explicit edits
    and deletes reach every copy. Members with memory disabled get no copy.
    The adapter serves team scopes only; agent scopes resolve to the agent's
    own backend.
    """

    file_backend: FileMemoryBackend
    mem0_backend: Mem0MemoryBackend
    members: TeamMemoryBackendMembers
    context_label: ClassVar[str] = "team"

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
        """Reject agent-scope writes."""
        del content, storage_path, config, metadata, execution_identity
        raise _agent_scope_error(agent_name)

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
        """Reject agent-scope searches."""
        del query, storage_path, config, limit, execution_identity
        raise _agent_scope_error(agent_name)

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
        """Reject agent-scope listings."""
        del storage_path, config, limit, preserve_resolved_storage_path, execution_identity
        raise _agent_scope_error(agent_name)

    def load_entrypoint_context(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Reject agent-scope entrypoint loads."""
        del storage_path, config, execution_identity
        raise _agent_scope_error(agent_name)

    async def _get_file_copy(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> MemoryResult | None:
        if not self.members.file:
            return None
        return await self.file_backend.get(
            memory_id,
            caller_context,
            storage_path,
            config,
            execution_identity=execution_identity,
            target_agent_names=self.members.file,
        )

    async def _get_mem0_copy(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> MemoryResult | None:
        if not self.members.mem0:
            return None
        return await self.mem0_backend.get(
            memory_id,
            caller_context,
            storage_path,
            config,
            execution_identity=execution_identity,
            target_agent_names=self.members.mem0,
        )

    async def _resolve_target_ids(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> _TeamMemoryTargetIds:
        """Find the copy of one team memory in each backend from either backend's ID.

        A file entry ID equals the Mem0 replica key of the same team memory.
        """
        file_result = await self._get_file_copy(memory_id, caller_context, storage_path, config, execution_identity)
        mem0_result = await self._get_mem0_copy(memory_id, caller_context, storage_path, config, execution_identity)
        file_id = memory_id if file_result is not None else None
        if file_result is None and mem0_result is not None and (replica_key := mem0_replica_key(mem0_result)):
            file_result = await self._get_file_copy(
                replica_key,
                caller_context,
                storage_path,
                config,
                execution_identity,
            )
            file_id = replica_key if file_result is not None else None
        mem0_id = memory_id if mem0_result is not None else None
        if mem0_result is None and file_id is not None:
            mem0_result = await self._get_mem0_copy(file_id, caller_context, storage_path, config, execution_identity)
            mem0_id = file_id if mem0_result is not None else None
        return _TeamMemoryTargetIds(file=file_id, mem0=mem0_id)

    async def get(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one team memory from the first backend that holds it."""
        file_result = await self._get_file_copy(memory_id, caller_context, storage_path, config, execution_identity)
        if file_result is not None:
            return file_result
        return await self._get_mem0_copy(memory_id, caller_context, storage_path, config, execution_identity)

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
        """Update one team memory in every backend that holds a copy."""
        target_ids = await self._resolve_target_ids(memory_id, caller_context, storage_path, config, execution_identity)
        if target_ids.file is None and target_ids.mem0 is None:
            raise MemoryNotFoundError(memory_id)
        if target_ids.file is not None:
            await self.file_backend.update(
                target_ids.file,
                content,
                caller_context,
                storage_path,
                config,
                execution_identity=execution_identity,
                target_agent_names=self.members.file,
            )
        if target_ids.mem0 is not None:
            await self.mem0_backend.update(
                target_ids.mem0,
                content,
                caller_context,
                storage_path,
                config,
                execution_identity=execution_identity,
                target_agent_names=self.members.mem0,
            )

    async def delete(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one team memory from every backend that holds a copy."""
        target_ids = await self._resolve_target_ids(memory_id, caller_context, storage_path, config, execution_identity)
        if target_ids.file is None and target_ids.mem0 is None:
            raise MemoryNotFoundError(memory_id)
        if target_ids.file is not None:
            await self.file_backend.delete(
                target_ids.file,
                caller_context,
                storage_path,
                config,
                execution_identity=execution_identity,
                target_agent_names=self.members.file,
            )
        if target_ids.mem0 is not None:
            await self.mem0_backend.delete(
                target_ids.mem0,
                caller_context,
                storage_path,
                config,
                execution_identity=execution_identity,
                target_agent_names=self.members.mem0,
            )

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
        """Write one team turn to each member backend under one shared replica key."""
        replica_key = new_memory_id()
        if self.members.file:
            await self.file_backend.store_conversation(
                prompt,
                agent_name,
                storage_path,
                session_id,
                config,
                thread_history=thread_history,
                user_id=user_id,
                execution_identity=execution_identity,
                target_agent_names=self.members.file,
                replica_key=replica_key,
            )
        if self.members.mem0:
            await self.mem0_backend.store_conversation(
                prompt,
                agent_name,
                storage_path,
                session_id,
                config,
                thread_history=thread_history,
                user_id=user_id,
                execution_identity=execution_identity,
                target_agent_names=self.members.mem0,
                replica_key=replica_key,
            )
