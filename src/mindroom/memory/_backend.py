"""Shared memory backend protocol and backend resolution."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar, Protocol

from mindroom.memory.config import create_memory_instance

from ._file_backend import FileMemoryBackend
from ._mem0_backend import Mem0MemoryBackend
from ._policy import caller_memory_backend

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    An agent reads its own scope and the scopes of its teams; each scope lives
    in one store, so reads and edits of one memory touch only that store.
    """

    context_label: ClassVar[str]

    async def add(
        self,
        content: str,
        agent_name: str,
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
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
        editable: bool = False,
    ) -> MemorySearchOutcome:
        """Search memories visible to an agent, including its team scopes.

        ``editable`` asks only for results whose IDs update and delete accept.
        """

    async def list_all(
        self,
        agent_name: str,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> list[MemoryResult]:
        """List memories stored for an agent scope."""

    async def get(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one memory of a scope the agent reads, or None."""

    async def update(
        self,
        memory_id: str,
        content: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Update one memory of a scope the agent reads."""

    async def delete(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one memory of a scope the agent reads."""

    async def store_conversation(
        self,
        prompt: str,
        caller: str | Sequence[str],
        session_id: str,
        config: Config,
        *,
        thread_history: Sequence[ResolvedVisibleMessage] | None = None,
        user_id: str | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Persist one conversation turn to the agent's scope, or to the scope of the team its members form."""

    def load_entrypoint_context(
        self,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Return the stable session preamble context for an agent whole, if any; ``cap_entrypoint`` cuts it."""


def resolve_memory_backend(
    caller: str | Sequence[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> ResolvedMemoryBackend | None:
    """Resolve the backend of an agent, or of the team its members form; None disables memory."""
    backend = caller_memory_backend(config, caller)
    if backend == "none":
        return None
    if backend == "file":
        return FileMemoryBackend(runtime_paths=runtime_paths)
    return Mem0MemoryBackend(
        runtime_paths=runtime_paths,
        create_memory=partial(create_memory_instance, runtime_paths=runtime_paths),
    )
