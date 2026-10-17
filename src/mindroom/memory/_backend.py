"""Shared memory backend protocol and backend resolution."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar, Protocol

from mindroom.memory.config import create_memory_instance

from ._file_backend import FileMemoryBackend
from ._mem0_backend import Mem0MemoryBackend
from ._partitioned_team_backend import PartitionedTeamMemoryBackend
from ._policy import (
    caller_uses_disabled_memory_backend,
    caller_uses_file_memory_backend,
    team_members_by_memory_backend,
    team_members_from_scope_user_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._shared import MemoryEntrypointContext, MemoryResult, MemorySearchOutcome


class ResolvedMemoryBackend(Protocol):
    """One resolved storage backend behind the public memory facade.

    Adapters implement every operation the facade dispatches, so call sites
    resolve a backend once and never branch on backend type again. Distinct
    from the ``MemoryBackend`` config literal, which names the authored choice.
    """

    context_label: ClassVar[str]

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
        """Add one memory for an agent scope."""

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
        """Search memories visible to an agent, including its team scopes."""

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
        """List memories stored for an agent scope."""

    async def get(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one memory visible to the caller, or None."""

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
        """Update one memory across its replica targets."""

    async def delete(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one memory across its replica targets."""

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
        """Persist one conversation turn to the agent or team scope."""

    def load_entrypoint_context(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Return the stable session preamble context for an agent, if any."""


def _mem0_memory_backend(runtime_paths: RuntimePaths) -> Mem0MemoryBackend:
    return Mem0MemoryBackend(
        runtime_paths=runtime_paths,
        create_memory=partial(create_memory_instance, runtime_paths=runtime_paths),
    )


def _partitioned_team_memory_backend(
    team_members: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> PartitionedTeamMemoryBackend | None:
    members = team_members_by_memory_backend(config, team_members)
    if not members.partitioned:
        return None
    return PartitionedTeamMemoryBackend(
        file_backend=FileMemoryBackend(runtime_paths=runtime_paths),
        mem0_backend=_mem0_memory_backend(runtime_paths),
        members=members,
    )


def resolve_memory_backend(
    caller_context: str | list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> ResolvedMemoryBackend | None:
    """Resolve the effective backend for one caller scope; None disables memory.

    A team context (list of member names) resolves to disabled only when every
    member is disabled, and to the file or mem0 backend when every member uses
    that backend. Any other team resolves to the partitioned team backend, which
    keeps each member's copy of team memory in that member's own backend.
    """
    if caller_uses_disabled_memory_backend(config, caller_context):
        return None
    if (
        isinstance(caller_context, list)
        and (team_backend := _partitioned_team_memory_backend(caller_context, config, runtime_paths)) is not None
    ):
        return team_backend
    if caller_uses_file_memory_backend(config, caller_context):
        return FileMemoryBackend(runtime_paths=runtime_paths)
    return _mem0_memory_backend(runtime_paths)


async def resolve_memory_mutation_backend(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[ResolvedMemoryBackend, str | list[str]] | None:
    """Resolve the backend and caller scope that mutate one memory; None disables memory.

    An agent editing a team memory of a partitioned team acts through the team
    scope, so the copies in every member backend change together.
    """
    if (backend := resolve_memory_backend(caller_context, config, runtime_paths)) is None:
        return None
    if isinstance(caller_context, list):
        return backend, caller_context
    memory = await backend.get(memory_id, caller_context, storage_path, config, execution_identity=execution_identity)
    if (
        memory is None
        or (scope_user_id := memory.get("user_id")) is None
        or (team_members := team_members_from_scope_user_id(scope_user_id, config)) is None
        or (team_backend := _partitioned_team_memory_backend(team_members, config, runtime_paths)) is None
    ):
        return backend, caller_context
    return team_backend, team_members
