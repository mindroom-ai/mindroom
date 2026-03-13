"""Workspace resolution and scaffolding helpers for agents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import STORAGE_PATH_OBJ, resolve_config_relative_path
from mindroom.tool_system.worker_routing import resolve_agent_state_storage_path

if TYPE_CHECKING:
    from mindroom.config.agent import AgentWorkspaceConfig, WorkspaceTemplate
    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_MIND_TEMPLATE_DIR = Path(__file__).resolve().parent / "cli" / "templates" / "mind_data"
_MIND_WORKSPACE_TEMPLATE_FILES: tuple[str, ...] = (
    "SOUL.md",
    "AGENTS.md",
    "USER.md",
    "IDENTITY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
)
_MIND_MEMORY_TEMPLATE = "# Memory\n\n"


@dataclass(frozen=True)
class ResolvedAgentWorkspace:
    """Resolved workspace paths for one agent in one execution scope."""

    root: Path
    context_files: tuple[Path, ...]
    file_memory_path: Path | None


def ensure_workspace_template(
    workspace_path: Path,
    *,
    template: WorkspaceTemplate,
    force: bool = False,
) -> None:
    """Scaffold a built-in workspace template when requested."""
    workspace_path.mkdir(parents=True, exist_ok=True)
    if template != "mind":
        msg = f"Unsupported workspace template: {template}"
        raise ValueError(msg)

    (workspace_path / "memory").mkdir(parents=True, exist_ok=True)

    for filename in _MIND_WORKSPACE_TEMPLATE_FILES:
        source_path = _MIND_TEMPLATE_DIR / filename
        file_path = workspace_path / filename
        if file_path.exists() and not force:
            continue
        file_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")

    memory_path = workspace_path / "MEMORY.md"
    if not memory_path.exists() or force:
        memory_path.write_text(_MIND_MEMORY_TEMPLATE, encoding="utf-8")


def _resolve_workspace_root(
    workspace: AgentWorkspaceConfig,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
) -> Path:
    if use_state_storage_path:
        return (state_storage_path / workspace.path).resolve()
    return resolve_config_relative_path(workspace.path)


def _resolve_workspace(
    agent_name: str,
    config: Config,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool,
) -> ResolvedAgentWorkspace | None:
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return None

    if agent_config.workspace is None:
        if agent_config.memory_file_path is None:
            return None
        legacy_root = resolve_config_relative_path(agent_config.memory_file_path)
        if create:
            legacy_root.mkdir(parents=True, exist_ok=True)
        return ResolvedAgentWorkspace(
            root=legacy_root,
            context_files=(),
            file_memory_path=legacy_root,
        )

    workspace = agent_config.workspace
    root = _resolve_workspace_root(
        workspace,
        state_storage_path=state_storage_path,
        use_state_storage_path=use_state_storage_path,
    )
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if workspace.template is not None:
            ensure_workspace_template(root, template=workspace.template)

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
) -> ResolvedAgentWorkspace | None:
    """Resolve one agent's effective workspace for the current execution scope."""
    resolved_base_storage_path = (base_storage_path or STORAGE_PATH_OBJ).expanduser().resolve()
    state_storage_path = resolve_agent_state_storage_path(
        agent_name=agent_name,
        base_storage_path=resolved_base_storage_path,
        config=config,
        execution_identity=execution_identity,
    ).resolve()
    return _resolve_workspace(
        agent_name,
        config,
        state_storage_path=state_storage_path,
        use_state_storage_path=state_storage_path != resolved_base_storage_path,
        create=create,
    )


def resolve_agent_workspace_from_state_path(
    agent_name: str,
    config: Config,
    *,
    state_storage_path: Path,
    use_state_storage_path: bool,
    create: bool = False,
) -> ResolvedAgentWorkspace | None:
    """Resolve one agent workspace when the caller already knows the state root."""
    return _resolve_workspace(
        agent_name,
        config,
        state_storage_path=state_storage_path.expanduser().resolve(),
        use_state_storage_path=use_state_storage_path,
        create=create,
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
    if agent_config is None or agent_config.workspace is None:
        return None
    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        state_storage_path=state_storage_path,
        use_state_storage_path=use_state_storage_path,
        create=create,
    )
    return workspace.file_memory_path if workspace is not None else None
