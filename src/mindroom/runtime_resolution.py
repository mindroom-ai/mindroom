"""Authoritative runtime resolution for one agent materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.tool_system.worker_routing import (
    get_tool_execution_identity,
    resolve_agent_state_storage_path,
    resolve_execution_identity_for_worker_scope,
    resolve_worker_key,
)
from mindroom.workspaces import (
    ResolvedAgentWorkspace,
    resolve_agent_private_state_storage_path,
    resolve_agent_workspace_from_state_path,
    resolve_workspace_relative_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope


@dataclass(frozen=True)
class ResolvedAgentRuntime:
    """Resolved runtime state for one `(agent_name, execution_identity)` materialization."""

    agent_name: str
    is_private: bool
    worker_scope: WorkerScope | None
    execution_identity: ToolExecutionIdentity | None
    worker_key: str | None
    state_root: Path
    workspace: ResolvedAgentWorkspace | None
    tool_base_dir: Path | None
    file_memory_root: Path | None


@dataclass(frozen=True)
class ResolvedKnowledgeBinding:
    """Resolved storage and watcher behavior for one knowledge base in one execution scope."""

    base_id: str
    storage_root: Path
    knowledge_path: Path
    start_background_watchers: bool
    incremental_sync_on_access: bool


def _resolved_execution_identity(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ToolExecutionIdentity | None:
    worker_scope = config.get_agent_worker_scope(agent_name)
    return resolve_execution_identity_for_worker_scope(
        worker_scope,
        agent_name=agent_name,
        execution_identity=execution_identity,
        tenant_id=runtime_paths.env_value("CUSTOMER_ID"),
        account_id=runtime_paths.env_value("ACCOUNT_ID"),
    )


def resolved_worker_private_agent_names(agent_runtime: ResolvedAgentRuntime) -> frozenset[str] | None:
    """Return explicit user-agent visibility derived from one resolved runtime."""
    if agent_runtime.worker_scope != "user_agent":
        return None
    if agent_runtime.is_private:
        return frozenset({agent_runtime.agent_name})
    return frozenset()


def resolve_agent_runtime(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> ResolvedAgentRuntime:
    """Resolve one agent's canonical runtime roots for the current execution scope."""
    agent_config = config.get_agent(agent_name)
    effective_execution_identity = execution_identity or get_tool_execution_identity()
    worker_scope = config.get_agent_worker_scope(agent_name)
    is_private = agent_config.private is not None
    resolved_execution_identity = _resolved_execution_identity(
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=effective_execution_identity,
    )

    worker_key: str | None = None
    if worker_scope is not None and resolved_execution_identity is not None:
        worker_key = resolve_worker_key(
            worker_scope,
            resolved_execution_identity,
            agent_name=agent_name,
        )
    if is_private:
        if resolved_execution_identity is None:
            msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
            raise ValueError(msg)
        if worker_key is None:
            msg = f"Private agent '{agent_name}' could not resolve a worker key for scope '{worker_scope}'"
            raise ValueError(msg)
        state_root = resolve_agent_private_state_storage_path(
            agent_name,
            config,
            base_storage_path=runtime_paths.storage_root,
            execution_identity=resolved_execution_identity,
        )
    else:
        state_root = resolve_agent_state_storage_path(
            agent_name=agent_name,
            base_storage_path=runtime_paths.storage_root,
        ).resolve()

    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=state_root,
        use_state_storage_path=is_private,
        create=create,
    )
    tool_base_dir = workspace.root if workspace is not None else None
    file_memory_root = workspace.file_memory_path if workspace is not None else None
    return ResolvedAgentRuntime(
        agent_name=agent_name,
        is_private=is_private,
        worker_scope=worker_scope,
        execution_identity=resolved_execution_identity,
        worker_key=worker_key,
        state_root=state_root,
        workspace=workspace,
        tool_base_dir=tool_base_dir,
        file_memory_root=file_memory_root,
    )


def resolve_knowledge_binding(
    base_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    start_watchers: bool = True,
    create: bool = False,
) -> ResolvedKnowledgeBinding:
    """Resolve one knowledge base to its effective storage and workspace-derived path."""
    base_config = config.get_knowledge_base_config(base_id)
    effective_agent_name = config.get_private_knowledge_base_agent(base_id)
    if effective_agent_name is None:
        knowledge_path = resolve_config_relative_path(base_config.path, runtime_paths).resolve()
        start_background_watchers = start_watchers and base_config.watch
        return ResolvedKnowledgeBinding(
            base_id=base_id,
            storage_root=runtime_paths.storage_root.expanduser().resolve(),
            knowledge_path=knowledge_path,
            start_background_watchers=start_background_watchers,
            incremental_sync_on_access=False,
        )

    agent_runtime = resolve_agent_runtime(
        effective_agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    if agent_runtime.workspace is None:
        msg = f"Knowledge base '{base_id}' requires agent '{effective_agent_name}' to define a private root"
        raise ValueError(msg)

    uses_isolating_worker_scope = agent_runtime.worker_scope not in {None, "shared"}
    start_background_watchers = start_watchers and base_config.watch and not uses_isolating_worker_scope
    return ResolvedKnowledgeBinding(
        base_id=base_id,
        storage_root=agent_runtime.state_root,
        knowledge_path=resolve_workspace_relative_path(
            agent_runtime.workspace.root,
            base_config.path,
            field_name=f"knowledge base '{base_id}' path",
        ),
        start_background_watchers=start_background_watchers,
        incremental_sync_on_access=start_watchers and base_config.watch and uses_isolating_worker_scope,
    )
