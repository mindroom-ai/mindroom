"""Workspace resolution and scaffolding helpers for agents."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import STORAGE_PATH_OBJ, resolve_config_relative_path
from mindroom.tool_system.worker_routing import (
    resolve_agent_state_storage_path,
    resolve_execution_identity_for_worker_scope,
    resolve_worker_key,
    worker_root_path,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_MIND_TEMPLATE_DIR = Path(__file__).resolve().parent / "cli" / "templates" / "mind_data"


@dataclass(frozen=True)
class ResolvedAgentWorkspace:
    """Resolved workspace paths for one agent in one execution scope."""

    root: Path
    context_files: tuple[Path, ...]
    file_memory_path: Path | None


@dataclass(frozen=True)
class _EffectiveAgentWorkspace:
    root_path: str
    template_dir: Path | None
    context_files: tuple[str, ...]
    file_memory_path: str | None


def copy_workspace_template(
    workspace_path: Path,
    *,
    template_dir: Path,
    force: bool = False,
) -> None:
    """Copy a local template directory into a workspace root."""
    workspace_path.mkdir(parents=True, exist_ok=True)
    resolved_template_dir = template_dir.expanduser().resolve()
    if not resolved_template_dir.is_dir():
        msg = f"Workspace template directory does not exist: {resolved_template_dir}"
        raise ValueError(msg)

    for source_path in sorted(resolved_template_dir.rglob("*")):
        relative_path = source_path.relative_to(resolved_template_dir)
        destination_path = workspace_path / relative_path
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        if destination_path.exists() and not force:
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)


def ensure_workspace_template(
    workspace_path: Path,
    *,
    template: str,
    force: bool = False,
) -> None:
    """Create the built-in Mind workspace template used by config init."""
    if template != "mind":
        msg = f"Unsupported workspace template: {template}"
        raise ValueError(msg)
    copy_workspace_template(workspace_path, template_dir=_MIND_TEMPLATE_DIR, force=force)
    (workspace_path / "memory").mkdir(parents=True, exist_ok=True)


def _private_root_name(agent_name: str, config: Config) -> str:
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None or agent_config.private.root is None:
        return f"{agent_name}_data"
    return agent_config.private.root


def _effective_workspace(
    agent_name: str,
    config: Config,
    *,
    config_path: Path | None = None,
) -> _EffectiveAgentWorkspace | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None:
        return None
    private_config = agent_config.private
    return _EffectiveAgentWorkspace(
        root_path=_private_root_name(agent_name, config),
        template_dir=(
            resolve_config_relative_path(private_config.template_dir, config_path=config_path)
            if private_config.template_dir is not None
            else None
        ),
        context_files=tuple(private_config.context_files or ()),
        file_memory_path="." if config.get_agent_memory_backend(agent_name) == "file" else None,
    )


def _resolve_workspace_execution_identity(
    agent_name: str,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> ToolExecutionIdentity | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None:
        return execution_identity

    worker_scope = config.get_agent_worker_scope(agent_name)
    resolved_identity = resolve_execution_identity_for_worker_scope(
        worker_scope,
        agent_name=agent_name,
        execution_identity=execution_identity,
    )
    if resolved_identity is None:
        msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
        raise ValueError(msg)
    return resolved_identity


def resolve_agent_private_state_storage_path(
    agent_name: str,
    config: Config,
    *,
    base_storage_path: Path,
    execution_identity: ToolExecutionIdentity | None,
) -> Path:
    """Return the requester-scoped state root for one private agent instance."""
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None:
        return resolve_agent_state_storage_path(
            agent_name=agent_name,
            base_storage_path=base_storage_path,
        ).resolve()

    resolved_identity = _resolve_workspace_execution_identity(
        agent_name,
        config,
        execution_identity,
    )
    if resolved_identity is None:
        msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
        raise ValueError(msg)

    worker_key = resolve_worker_key(
        agent_config.private.per,
        resolved_identity,
        agent_name=agent_name,
    )
    if worker_key is None:
        msg = f"Private agent '{agent_name}' could not resolve a worker key for scope '{agent_config.private.per}'"
        raise ValueError(msg)
    return worker_root_path(base_storage_path, worker_key).resolve()


def _resolve_workspace(
    agent_name: str,
    config: Config,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool,
    config_path: Path | None = None,
) -> ResolvedAgentWorkspace | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return None

    workspace = _effective_workspace(agent_name, config, config_path=config_path)
    if workspace is None:
        if agent_config.memory_file_path is None:
            return None
        legacy_root = resolve_config_relative_path(agent_config.memory_file_path, config_path=config_path)
        if create:
            legacy_root.mkdir(parents=True, exist_ok=True)
        return ResolvedAgentWorkspace(
            root=legacy_root,
            context_files=(),
            file_memory_path=legacy_root,
        )

    if not use_state_storage_path:
        msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
        raise ValueError(msg)

    root = (state_storage_path / workspace.root_path).resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if workspace.template_dir is not None:
            copy_workspace_template(root, template_dir=workspace.template_dir)

    context_files = tuple((root / relative_path).resolve() for relative_path in workspace.context_files)
    file_memory_path = (root / workspace.file_memory_path).resolve() if workspace.file_memory_path is not None else None
    if create and file_memory_path is not None:
        file_memory_path.mkdir(parents=True, exist_ok=True)

    return ResolvedAgentWorkspace(
        root=root,
        context_files=context_files,
        file_memory_path=file_memory_path,
    )


def resolve_agent_workspace(
    agent_name: str,
    config: Config,
    *,
    base_storage_path: Path | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
    config_path: Path | None = None,
) -> ResolvedAgentWorkspace | None:
    """Resolve one agent's effective workspace for the current execution scope."""
    resolved_base_storage_path = (base_storage_path or STORAGE_PATH_OBJ).expanduser().resolve()
    state_storage_path = resolve_agent_private_state_storage_path(
        agent_name,
        config,
        base_storage_path=resolved_base_storage_path,
        execution_identity=execution_identity,
    )
    agent_config = config.agents.get(agent_name)
    return _resolve_workspace(
        agent_name,
        config,
        state_storage_path=state_storage_path,
        use_state_storage_path=agent_config is not None and agent_config.private is not None,
        create=create,
        config_path=config_path,
    )


def resolve_agent_workspace_from_state_path(
    agent_name: str,
    config: Config,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool = False,
    config_path: Path | None = None,
) -> ResolvedAgentWorkspace | None:
    """Resolve one agent workspace when the caller already knows the state root."""
    return _resolve_workspace(
        agent_name,
        config,
        state_storage_path=state_storage_path.expanduser().resolve(),
        use_state_storage_path=use_state_storage_path,
        create=create,
        config_path=config_path,
    )


def resolve_agent_file_memory_path(
    agent_name: str,
    config: Config,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool = False,
) -> Path | None:
    """Resolve the effective file-memory path for an agent."""
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None:
        return None
    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        state_storage_path=state_storage_path,
        use_state_storage_path=use_state_storage_path,
        create=create,
    )
    return workspace.file_memory_path if workspace is not None else None
