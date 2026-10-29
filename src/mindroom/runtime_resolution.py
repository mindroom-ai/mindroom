"""Authoritative runtime resolution for one agent materialization."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.config.resolution import agent_knowledge_base_ids, agent_policy
from mindroom.constants import (
    RuntimePaths,
    config_relative_path,
    resolve_config_relative_path,
    resolve_config_relative_path_preserving_leaf,
    resolve_session_state_root,
)
from mindroom.private_storage.private_instance_identity_store import ensure_private_instance_identity
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    private_instance_scope_root_path,
    resolve_agent_state_storage_path,
    resolve_worker_key,
    shared_storage_root,
    written_by_other_workers,
)
from mindroom.workspaces import (
    ResolvedAgentWorkspace,
    ensure_workspace_knowledge_links,
    resolve_agent_workspace_from_state_path,
    resolve_relative_path_within_root,
    resolve_workspace_relative_path,
    runs_in_dedicated_worker,
)

if TYPE_CHECKING:
    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope


@dataclass(frozen=True)
class ResolvedAgentExecution:
    """Resolved execution scope for one `(agent_name, execution_identity)` materialization.

    ``worker_target`` is the agent's own worker target, which its toolkits and every operation on its behalf use.
    """

    agent_name: str
    policy: ResolvedAgentPolicy
    worker_target: ResolvedWorkerTarget

    @property
    def execution_scope(self) -> WorkerScope | None:
        """Return the agent's effective execution scope."""
        return self.policy.effective_execution_scope

    @property
    def execution_identity(self) -> ToolExecutionIdentity | None:
        """Return the execution identity the worker target resolved."""
        return self.worker_target.execution_identity

    @property
    def worker_key(self) -> str | None:
        """Return the worker key of the agent's worker target."""
        return self.worker_target.worker_key

    @property
    def is_private(self) -> bool:
        """Return whether the resolved execution uses a private agent definition."""
        return self.policy.is_private


@dataclass(frozen=True)
class ResolvedAgentRuntime:
    """Resolved runtime state for one `(agent_name, execution_identity)` materialization."""

    execution: ResolvedAgentExecution
    state_root: Path
    session_state_root: Path
    workspace: ResolvedAgentWorkspace


@dataclass(frozen=True)
class ResolvedAgentStorage:
    """Purely resolved state roots for one agent execution without workspace reconciliation."""

    execution: ResolvedAgentExecution
    state_root: Path
    session_state_root: Path


@dataclass(frozen=True)
class ResolvedKnowledgeBinding:
    """Resolved storage and watcher behavior for one knowledge base in one execution scope."""

    base_id: str
    storage_root: Path
    knowledge_path: Path
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
    requester_worker_key: str | None = worker_key
    if execution_scope == "user_agent":
        requester_worker_key = resolve_worker_key("user", execution_identity, agent_name=None)
        if requester_worker_key is None:
            msg = "Requester-scoped private root requires a requester identity"
            raise ValueError(msg)
    if requester_worker_key is None:
        msg = "Requester-scoped private root requires a worker key"
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
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ResolvedAgentExecution:
    """Resolve one agent's execution scope for the current runtime context."""
    policy = agent_policy(config, agent_name)
    execution_scope = policy.effective_execution_scope
    if policy.is_private:
        if execution_identity is None:
            msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
            raise ValueError(msg)
        if not execution_identity.requester_id or not execution_identity.requester_id.strip():
            msg = f"Private agent '{agent_name}' requires a requester identity to resolve requester-local state"
            raise ValueError(msg)
        # MindRoom's own accounts act for no one, so they never own a private instance.
        if not is_access_checked_requester_id(execution_identity.requester_id, config, runtime_paths):
            msg = f"Private agent '{agent_name}' requires a human or bot-account requester to resolve requester-local state"
            raise ValueError(msg)
    worker_target = build_agent_toolkit_worker_target(
        execution_scope,
        agent_name,
        is_private=policy.is_private,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
    )
    if policy.is_private and worker_target.worker_key is None:
        msg = f"Private agent '{agent_name}' could not resolve a worker key for execution scope '{execution_scope}'"
        raise ValueError(msg)
    return ResolvedAgentExecution(agent_name=agent_name, policy=policy, worker_target=worker_target)


def record_private_instance_owner(
    agent_name: str,
    resolved_execution: ResolvedAgentExecution,
    runtime_paths: RuntimePaths,
) -> None:
    """Record who owns a private instance before the primary stores its first data there.

    A scope that already holds data when its record is first written stays undiscoverable, so every primary path
    that creates private state calls this first.
    """
    # The identity record sits in the private scope above the workspace, which
    # dedicated workers never mount; only the primary creates or locks it.
    if not resolved_execution.policy.private_workspace_enabled or runs_in_dedicated_worker(runtime_paths):
        return
    execution_identity = resolved_execution.execution_identity
    worker_key = resolved_execution.worker_key
    if execution_identity is None or worker_key is None or execution_identity.requester_id is None:
        msg = f"Private agent '{agent_name}' has unresolved private execution state"
        raise ValueError(msg)
    ensure_private_instance_identity(
        runtime_paths.storage_root,
        worker_key=worker_key,
        requester_id=execution_identity.requester_id,
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
    resolved_storage = resolve_agent_storage(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    resolved_execution = resolved_storage.execution
    state_root = resolved_storage.state_root
    if create:
        record_private_instance_owner(agent_name, resolved_execution, runtime_paths)

    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=state_root,
        use_state_storage_path=resolved_execution.policy.private_workspace_enabled,
        create=create,
    )
    if create or workspace.root.exists():
        workspace_knowledge_root = resolve_workspace_relative_path(
            workspace.root,
            "knowledge",
            field_name="workspace knowledge root",
        )
        protected_knowledge_paths = {
            configured_path
            for base_config in config.knowledge_bases.values()
            if (
                configured_path := resolve_config_relative_path_preserving_leaf(base_config.path, runtime_paths)
            ).is_relative_to(workspace_knowledge_root)
        }
        knowledge_paths: dict[str, Path] = {}
        for base_id in agent_knowledge_base_ids(config, agent_name):
            base_config = config.get_knowledge_base_config(base_id)
            if config.get_private_knowledge_base_agent(base_id) is None:
                knowledge_paths[base_id] = resolve_config_relative_path(base_config.path, runtime_paths).resolve()
            else:
                knowledge_paths[base_id] = resolve_workspace_relative_path(
                    workspace.root,
                    base_config.path,
                    field_name="private.knowledge.path",
                )
        ensure_workspace_knowledge_links(
            workspace.root,
            knowledge_paths=knowledge_paths,
            protected_paths=protected_knowledge_paths,
        )
    return ResolvedAgentRuntime(
        execution=resolved_execution,
        state_root=state_root,
        session_state_root=resolved_storage.session_state_root,
        workspace=workspace,
    )


def resolve_agent_storage(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ResolvedAgentStorage:
    """Resolve canonical state roots without creating or reconciling workspace content."""
    resolved_execution = resolve_agent_execution(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    if resolved_execution.policy.private_workspace_enabled:
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
    return ResolvedAgentStorage(
        execution=resolved_execution,
        state_root=state_root,
        session_state_root=resolve_session_state_root(state_root, runtime_paths),
    )


def shared_knowledge_path(raw_path: str, runtime_paths: RuntimePaths) -> Path:
    """Resolve one shared knowledge path, following no link below storage that workers write; others follow links."""
    lexical = Path(os.path.normpath(config_relative_path(raw_path, runtime_paths)))
    storage_root = shared_storage_root(runtime_paths.storage_root)
    lexical_storage_root = Path(os.path.normpath(runtime_paths.storage_root.expanduser().absolute()))
    for spelling in dict.fromkeys((lexical_storage_root, storage_root)):
        if lexical.is_relative_to(spelling) and written_by_other_workers(relative := lexical.relative_to(spelling)):
            return resolve_workspace_relative_path(storage_root, relative, field_name="shared knowledge base path")
    return resolve_config_relative_path(raw_path, runtime_paths).resolve()


def _private_knowledge_index_root(runtime_paths: RuntimePaths, state_root: Path) -> Path:
    """Return the primary-only directory one requester's private knowledge indexes live under.

    Not the agent's state root, which its static runner can write: indexes sit beside the other bases' in
    ``knowledge_db``, as knowledge Git directories sit outside worker storage. The canonical state root names the
    requester's namespace there, so no requester's index can share a directory with another's.
    """
    namespace = hashlib.sha256(str(state_root).encode("utf-8")).hexdigest()[:32]
    return runtime_paths.storage_root.expanduser().resolve() / "knowledge_db" / "private" / namespace


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
    effective_agent_name = config.get_private_knowledge_base_agent(base_id)
    if effective_agent_name is None:
        knowledge_path = shared_knowledge_path(base_config.path, runtime_paths)
        return ResolvedKnowledgeBinding(
            base_id=base_id,
            storage_root=runtime_paths.storage_root.expanduser().resolve(),
            knowledge_path=knowledge_path,
            # Polled shared bases (Git, or local with poll_interval_seconds) refresh through STALE scheduling
            # after their interval; Git bases never use READY access scheduling.
            incremental_sync_on_access=base_config.watch and base_config.git is None and not start_watchers,
        )

    agent_runtime = resolve_agent_runtime(
        effective_agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    return ResolvedKnowledgeBinding(
        base_id=base_id,
        storage_root=_private_knowledge_index_root(runtime_paths, agent_runtime.state_root),
        knowledge_path=resolve_workspace_relative_path(
            agent_runtime.workspace.root,
            base_config.path,
            field_name=f"knowledge base '{base_id}' path",
        ),
        incremental_sync_on_access=(
            refresh_enabled and (agent_runtime.execution.policy.private_agent_knowledge_enabled or not start_watchers)
        ),
    )


__all__ = [
    "ResolvedAgentExecution",
    "ResolvedAgentRuntime",
    "ResolvedAgentStorage",
    "ResolvedKnowledgeBinding",
    "record_private_instance_owner",
    "resolve_agent_execution",
    "resolve_agent_runtime",
    "resolve_agent_storage",
    "resolve_knowledge_binding",
    "resolve_private_requester_scope_root",
    "shared_knowledge_path",
]
