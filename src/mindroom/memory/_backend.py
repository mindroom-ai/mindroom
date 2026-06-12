"""Shared memory backend protocol and backend resolution."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar, Protocol

from mindroom.memory.config import create_memory_instance

from ._file_backend import FileMemoryBackend
from ._mem0_backend import Mem0MemoryBackend
from ._policy import caller_uses_disabled_memory_backend, caller_uses_file_memory_backend

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    from ._shared import MemoryResult


class MemoryBackend(Protocol):
    """One storage backend behind the public memory facade.

    Adapters implement every operation the facade dispatches, so call sites
    resolve a backend once and never branch on backend type again.
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
        timing_scope: str | None = None,
    ) -> list[MemoryResult]:
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
        timing_scope: str | None = None,
    ) -> str:
        """Return the stable session preamble context for an agent, if any."""


def resolve_memory_backend(
    caller_context: str | list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> MemoryBackend | None:
    """Resolve the effective backend for one caller scope; None disables memory.

    A team context (list of member names) resolves to the file backend only
    when every member is file-backed, and to disabled only when every member
    is disabled; otherwise it resolves to mem0.
    """
    if caller_uses_disabled_memory_backend(config, caller_context):
        return None
    if caller_uses_file_memory_backend(config, caller_context):
        return FileMemoryBackend(runtime_paths=runtime_paths)
    return Mem0MemoryBackend(
        runtime_paths=runtime_paths,
        create_memory=partial(create_memory_instance, runtime_paths=runtime_paths),
    )
