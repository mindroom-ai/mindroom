"""Workspace resolution and scaffolding helpers for agents."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, resolve_config_relative_path

if TYPE_CHECKING:
    from mindroom.config.main import Config

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


def resolve_relative_path_within_root(
    root: Path,
    relative_path: str | Path,
    *,
    field_name: str,
    root_label: str = "canonical root",
) -> Path:
    """Resolve one relative path under a canonical root and reject symlink escapes."""
    lexical_root = root.expanduser()
    resolved_root = lexical_root.resolve()
    candidate_path = lexical_root / relative_path
    current = lexical_root
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            msg = f"{field_name} must stay within the {root_label}: {resolved_root}"
            raise ValueError(msg)
    candidate = candidate_path.resolve()
    if not candidate.is_relative_to(resolved_root):
        msg = f"{field_name} must stay within the {root_label}: {resolved_root}"
        raise ValueError(msg)
    return candidate


def resolve_workspace_relative_path(
    root: Path,
    relative_path: str | Path,
    *,
    field_name: str,
) -> Path:
    """Resolve one workspace-relative path and reject symlink escapes."""
    return resolve_relative_path_within_root(
        root,
        relative_path,
        field_name=field_name,
        root_label="workspace root",
    )


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
        shutil.copy2(source_path, destination_path)


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
    runtime_paths: RuntimePaths,
) -> _EffectiveAgentWorkspace | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None or agent_config.private is None:
        return None
    private_config = agent_config.private
    return _EffectiveAgentWorkspace(
        root_path=_private_root_name(agent_name, config),
        template_dir=(
            resolve_config_relative_path(private_config.template_dir, runtime_paths)
            if private_config.template_dir is not None
            else None
        ),
        context_files=tuple(private_config.context_files or ()),
        file_memory_path="." if config.get_agent_memory_backend(agent_name) == "file" else None,
    )


def _resolve_workspace(
    agent_name: str,
    config: Config,
    *,
    runtime_paths: RuntimePaths,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool,
) -> ResolvedAgentWorkspace | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return None

    if agent_config.private is None:
        if config.get_agent_memory_backend(agent_name) != "file":
            return None
        root = resolve_workspace_relative_path(
            state_storage_path,
            "workspace",
            field_name="agent workspace root",
        )
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return ResolvedAgentWorkspace(
            root=root,
            context_files=(),
            file_memory_path=root,
        )

    workspace = _effective_workspace(agent_name, config, runtime_paths=runtime_paths)
    assert workspace is not None

    if not use_state_storage_path:
        msg = f"Private agent '{agent_name}' requires an active execution identity to resolve requester-local state"
        raise ValueError(msg)

    root = resolve_workspace_relative_path(
        state_storage_path,
        workspace.root_path,
        field_name="private.root",
    )
    template_dir = workspace.template_dir
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if template_dir is not None:
            assert template_dir is not None
            copy_workspace_template(root, template_dir=template_dir)

    context_files = tuple(
        resolve_workspace_relative_path(
            root,
            relative_path,
            field_name="private.context_files",
        )
        for relative_path in workspace.context_files
    )
    file_memory_path = (
        resolve_workspace_relative_path(
            root,
            workspace.file_memory_path,
            field_name="private file memory path",
        )
        if workspace.file_memory_path is not None
        else None
    )
    if create and file_memory_path is not None:
        file_memory_path.mkdir(parents=True, exist_ok=True)

    return ResolvedAgentWorkspace(
        root=root,
        context_files=context_files,
        file_memory_path=file_memory_path,
    )


def resolve_agent_workspace_from_state_path(
    agent_name: str,
    config: Config,
    *,
    runtime_paths: RuntimePaths,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool = False,
) -> ResolvedAgentWorkspace | None:
    """Resolve one agent workspace when the caller already knows the state root."""
    return _resolve_workspace(
        agent_name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=state_storage_path.expanduser(),
        use_state_storage_path=use_state_storage_path,
        create=create,
    )
