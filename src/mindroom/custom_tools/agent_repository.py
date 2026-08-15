"""Argument-free trusted tool for one agent-owned GitHub repository."""

from __future__ import annotations

from asyncio import Lock
from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agent_repositories import (
    AgentVaultRepositoryBroker,
    RepositoryBinding,
    RepositoryBindingError,
    RepositoryBindingStore,
    RepositoryBroker,
    RepositoryBrokerError,
    RepositoryEnsureRequest,
    RepositoryLease,
    RepositoryOriginConflictError,
    configure_repository_workspace,
    derive_repository_name,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class AgentRepositoryTools(Toolkit):
    """Ensure the repository bound to the current trusted worker identity."""

    def __init__(
        self,
        *,
        organization: str,
        prefix: str,
        runtime_paths: RuntimePaths,
        worker_target: ResolvedWorkerTarget | None,
        tool_output_workspace_root: Path | None,
        broker: RepositoryBroker | None = None,
    ) -> None:
        self._organization = organization
        self._prefix = prefix
        self._worker_target = worker_target
        self._workspace = tool_output_workspace_root
        self._broker = broker or AgentVaultRepositoryBroker.from_runtime(runtime_paths)
        self._binding_store = RepositoryBindingStore(runtime_paths)
        self._ensure_lock = Lock()
        super().__init__(name="agent_repository", tools=[self.ensure_my_repository])

    def _context(self) -> tuple[ResolvedWorkerTarget, str, Path]:
        target = self._worker_target
        if target is None or not target.worker_key:
            msg = "Agent repository requires an authoritative worker identity"
            raise RepositoryBindingError(msg)
        if self._workspace is None:
            msg = "Agent repository requires an agent workspace"
            raise RepositoryBindingError(msg)
        return target, target.worker_key, self._workspace

    async def ensure_my_repository(self) -> str:
        """Ensure this agent's one private repository and configure the current Git workspace."""
        async with self._ensure_lock:
            try:
                target, worker_key, workspace_root = self._context()
                repository_name = derive_repository_name(
                    prefix=self._prefix,
                    worker_target=target,
                )
                request = RepositoryEnsureRequest(
                    worker_key=worker_key,
                    organization=self._organization,
                    repository_name=repository_name,
                )
                lease: RepositoryLease = await self._broker.ensure_repository(request)
                binding: RepositoryBinding = self._binding_store.bind(request, lease)
                workspace = configure_repository_workspace(
                    workspace=workspace_root,
                    clone_url=binding.clone_url,
                    lock_path=self._binding_store.workspace_lock_path(worker_key),
                )
            except RepositoryOriginConflictError as exc:
                return custom_tool_payload("agent_repository", "origin_conflict", error=str(exc))
            except (RepositoryBindingError, RepositoryBrokerError, OSError) as exc:
                return custom_tool_payload("agent_repository", "error", error=str(exc))

            return custom_tool_payload(
                "agent_repository",
                "ok",
                repository_id=binding.repository_id,
                organization=binding.organization,
                repository_name=binding.repository_name,
                clone_url=binding.clone_url,
                workspace=str(workspace),
            )
