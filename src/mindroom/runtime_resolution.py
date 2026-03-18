"""Authoritative runtime resolution for one agent materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.agent_policy import (
    ResolvedAgentPolicy,
    build_agent_policy_seeds,
    resolve_agent_policy_from_data,
    resolve_private_knowledge_base_agent,
)
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.tool_system.worker_routing import (
    private_instance_scope_root_path,
    resolve_agent_state_storage_path,
    resolve_worker_execution_scope,
    resolve_worker_key,
)
from mindroom.workspaces import (
    ResolvedAgentWorkspace,
    resolve_agent_workspace_from_state_path,
    resolve_relative_path_within_root,
    resolve_workspace_relative_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope


@dataclass(frozen=True)
class _ResolvedAgentExecution:
    """Resolved execution scope for one `(agent_name, execution_identity)` materialization."""

    agent_name: str
    policy: ResolvedAgentPolicy
    execution_scope: WorkerScope | None
    execution_identity: ToolExecutionIdentity | None
    worker_key: str | None

    @property
    def is_private(self) -> bool:
        """Return whether the resolved execution uses a private agent definition."""
        return self.policy.is_private


@dataclass(frozen=True)
class ResolvedAgentRuntime:
    """Resolved runtime state for one `(agent_name, execution_identity)` materialization."""

    agent_name: str
    policy: ResolvedAgentPolicy
    execution_scope: WorkerScope | None
    execution_identity: ToolExecutionIdentity | None
    worker_key: str | None
    state_root: Path
    workspace: ResolvedAgentWorkspace | None
    tool_base_dir: Path | None
    file_memory_root: Path | None

    @property
    def is_private(self) -> bool:
        """Return whether the resolved runtime uses a private agent definition."""
        return self.policy.is_private


@dataclass(frozen=True)
class ResolvedKnowledgeBinding:
    """Resolved storage and watcher behavior for one knowledge base in one execution scope."""

    base_id: str
    storage_root: Path
    knowledge_path: Path
    request_scoped: bool
    start_background_watchers: bool
    incremental_sync_on_access: bool


def _knowledge_refresh_enabled(
    *,
    file_watch_enabled: bool,
    has_git_sync: bool,
) -> bool:
    """Return whether a knowledge base has any refresh mechanism available."""
    return file_watch_enabled or has_git_sync


def _resolve_private_scope_root(
    *,
    runtime_paths: RuntimePaths,
    worker_key: str,
) -> Path:
    """Return one canonical requester-scoped private root and reject symlink escapes."""
    return resolve_relative_path_within_root(
        runtime_paths.storage_root,
        private_instance_scope_root_path(
            runtime_paths.storage_root,
            worker_key=worker_key,
        ).relative_to(runtime_paths.storage_root.expanduser().resolve()),
        field_name="Private scope root",
    )


def resolve_private_requester_scope_root(
    *,
    runtime_paths: RuntimePaths,
    execution_scope: WorkerScope,
    execution_identity: ToolExecutionIdentity,
    worker_key: str,
) -> Path:
    """Return the requester-scoped private root shared across same-requester agents."""
    requester_worker_key = worker_key
    if execution_scope == "user_agent":
        requester_worker_key = resolve_worker_key("user", execution_identity, agent_name=None)
        if requester_worker_key is None:
            msg = "Requester-scoped private root requires a requester identity"
            raise ValueError(msg)
    return _resolve_private_scope_root(
        runtime_paths=runtime_paths,
        worker_key=requester_worker_key,
    )


def _resolved_private_state_root(
    *,
    runtime_paths: RuntimePaths,
    worker_key: str,
    agent_name: str,
) -> Path:
    """Return one canonical private-instance state root and reject symlink escapes."""
    return resolve_relative_path_within_root(
        _resolve_private_scope_root(runtime_paths=runtime_paths, worker_key=worker_key),
        agent_name,
        field_name="Private state root",
        root_label="private scope root",
    )


def resolve_agent_execution(
    agent_name: str,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> _ResolvedAgentExecution:
    """Resolve one agent's execution scope for the current runtime context."""
    policy = resolve_agent_policy_from_data(
        agent_name,
        config.get_agent(agent_name),
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )
    execution_scope = policy.effective_execution_scope
    resolved_worker_execution = resolve_worker_execution_scope(
        execution_scope,
        agent_name=agent_name,
        execution_identity=execution_identity,
    )
    if policy.is_private:
        if resolved_worker_execution.execution_identity is None:
            msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
            raise ValueError(msg)
        if resolved_worker_execution.worker_key is None:
            msg = f"Private agent '{agent_name}' could not resolve a worker key for execution scope '{execution_scope}'"
            raise ValueError(msg)
    return _ResolvedAgentExecution(
        agent_name=agent_name,
        policy=policy,
        execution_scope=execution_scope,
        execution_identity=resolved_worker_execution.execution_identity,
        worker_key=resolved_worker_execution.worker_key,
    )


def resolve_agent_runtime(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    create: bool = False,
) -> ResolvedAgentRuntime:
    """Resolve one agent's canonical runtime roots for the current execution scope."""
    resolved_execution = resolve_agent_execution(
        agent_name,
        config,
        execution_identity=execution_identity,
    )
    if resolved_execution.policy.request_scoped_workspace_enabled:
        worker_key = resolved_execution.worker_key
        if worker_key is None:
            msg = f"Private agent '{agent_name}' could not resolve a worker key"
            raise ValueError(msg)
        state_root = _resolved_private_state_root(
            runtime_paths=runtime_paths,
            worker_key=worker_key,
            agent_name=agent_name,
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
        use_state_storage_path=resolved_execution.policy.request_scoped_workspace_enabled,
        create=create,
    )
    tool_base_dir = workspace.root if workspace is not None else None
    file_memory_root = workspace.file_memory_path if workspace is not None else None
    return ResolvedAgentRuntime(
        agent_name=agent_name,
        policy=resolved_execution.policy,
        execution_scope=resolved_execution.execution_scope,
        execution_identity=resolved_execution.execution_identity,
        worker_key=resolved_execution.worker_key,
        state_root=state_root,
        workspace=workspace,
        tool_base_dir=tool_base_dir,
        file_memory_root=file_memory_root,
    )


def resolve_knowledge_binding(
    base_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    *,
    start_watchers: bool = True,
    create: bool = False,
) -> ResolvedKnowledgeBinding:
    """Resolve one knowledge base to its effective storage and workspace-derived path."""
    base_config = config.get_knowledge_base_config(base_id)
    refresh_enabled = _knowledge_refresh_enabled(
        file_watch_enabled=base_config.watch,
        has_git_sync=base_config.git is not None,
    )
    effective_agent_name = resolve_private_knowledge_base_agent(
        base_id,
        build_agent_policy_seeds(
            config.agents,
            default_worker_scope=config.defaults.worker_scope,
        ),
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )
    if effective_agent_name is None:
        knowledge_path = resolve_config_relative_path(base_config.path, runtime_paths).resolve()
        return ResolvedKnowledgeBinding(
            base_id=base_id,
            storage_root=runtime_paths.storage_root.expanduser().resolve(),
            knowledge_path=knowledge_path,
            request_scoped=False,
            start_background_watchers=start_watchers and refresh_enabled,
            incremental_sync_on_access=refresh_enabled and not start_watchers,
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

    return ResolvedKnowledgeBinding(
        base_id=base_id,
        storage_root=agent_runtime.state_root,
        knowledge_path=resolve_workspace_relative_path(
            agent_runtime.workspace.root,
            base_config.path,
            field_name=f"knowledge base '{base_id}' path",
        ),
        request_scoped=agent_runtime.policy.request_scoped_knowledge_enabled,
        start_background_watchers=(
            start_watchers and refresh_enabled and not agent_runtime.policy.request_scoped_knowledge_enabled
        ),
        incremental_sync_on_access=(
            refresh_enabled and (agent_runtime.policy.request_scoped_knowledge_enabled or not start_watchers)
        ),
    )
