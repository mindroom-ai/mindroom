"""Memory scope and storage-root policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import (
    get_tool_execution_identity,
    resolve_agent_owned_path,
    resolve_agent_state_storage_path,
)
from mindroom.workspaces import resolve_agent_private_state_storage_path

from ._shared import FileMemoryResolution

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def use_file_memory_backend(config: Config, *, agent_name: str | None = None) -> bool:
    """Return whether the resolved backend is file-backed."""
    if agent_name is None:
        return config.memory.backend == "file"
    return config.get_agent_memory_backend(agent_name) == "file"


def caller_uses_file_memory_backend(config: Config, caller_context: str | list[str]) -> bool:
    """Return whether the caller context resolves to file-backed memory."""
    if isinstance(caller_context, str):
        return use_file_memory_backend(config, agent_name=caller_context)
    return team_uses_file_memory_backend(config, caller_context)


def team_uses_file_memory_backend(config: Config, agent_names: list[str]) -> bool:
    """Return whether all team members resolve to file-backed memory."""
    return all(use_file_memory_backend(config, agent_name=agent_name) for agent_name in agent_names)


def effective_storage_paths_for_context(
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> list[Path]:
    """Return the distinct storage roots affected by the caller context."""
    if isinstance(caller_context, str):
        return [_effective_storage_path_for_agent(caller_context, storage_path, config)]

    effective_paths: list[Path] = []
    for agent_name in caller_context:
        effective_path = _effective_storage_path_for_agent(agent_name, storage_path, config)
        if effective_path not in effective_paths:
            effective_paths.append(effective_path)
    return effective_paths or [storage_path]


def _effective_storage_path_for_agent(
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> Path:
    agent_config = config.agents.get(agent_name)
    if agent_config is not None and agent_config.private is not None:
        return resolve_agent_private_state_storage_path(
            agent_name,
            config,
            base_storage_path=storage_path,
            execution_identity=get_tool_execution_identity(),
        )
    return resolve_agent_state_storage_path(
        agent_name=agent_name,
        base_storage_path=storage_path,
    )


def build_team_user_id(agent_names: list[str]) -> str:
    """Create a stable team scope user ID from a set of agent names."""
    return f"team_{'+'.join(sorted(agent_names))}"


def agent_scope_user_id(agent_name: str) -> str:
    """Return the scoped memory user ID for one agent."""
    return f"agent_{agent_name}"


def agent_name_from_scope_user_id(scope_user_id: str) -> str | None:
    """Extract the agent name from an agent scope user ID."""
    if scope_user_id.startswith("agent_"):
        return scope_user_id[len("agent_") :]
    return None


def get_team_ids_for_agent(agent_name: str, config: Config) -> list[str]:
    """Get all team scope IDs that include the specified agent."""
    if not config.teams:
        return []
    return [
        build_team_user_id(team_config.agents)
        for team_config in config.teams.values()
        if agent_name in team_config.agents
    ]


def _team_members_from_scope_user_id(scope_user_id: str, config: Config) -> list[str] | None:
    if not scope_user_id.startswith("team_"):
        return None
    if config.teams:
        for team_config in config.teams.values():
            if build_team_user_id(team_config.agents) == scope_user_id:
                return list(team_config.agents)
    members = scope_user_id[len("team_") :].split("+")
    return members or None


def mutation_target_storage_paths(
    scope_user_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> list[Path]:
    """Return all storage roots that should reflect mutations for this scope."""
    if (team_members := _team_members_from_scope_user_id(scope_user_id, config)) is not None:
        return effective_storage_paths_for_context(team_members, storage_path, config)
    return effective_storage_paths_for_context(caller_context, storage_path, config)


def get_allowed_memory_user_ids(caller_context: str | list[str], config: Config) -> set[str]:
    """Get all user_id scopes the caller is allowed to access."""
    if isinstance(caller_context, list):
        allowed_user_ids = {build_team_user_id(caller_context)}
        if config.memory.team_reads_member_memory:
            allowed_user_ids.update(agent_scope_user_id(agent_name) for agent_name in caller_context)
        return allowed_user_ids

    allowed_user_ids = {agent_scope_user_id(caller_context)}
    allowed_user_ids.update(get_team_ids_for_agent(caller_context, config))
    return allowed_user_ids


def file_memory_resolution_from_paths(
    *,
    original_storage_path: Path,
    resolved_storage_path: Path,
    runtime_paths: RuntimePaths,
    preserve_resolved_storage_path: bool = False,
) -> FileMemoryResolution:
    """Build file-memory resolution settings from original and resolved roots."""
    if preserve_resolved_storage_path:
        return FileMemoryResolution(
            storage_path=resolved_storage_path,
            runtime_paths=runtime_paths,
            use_configured_path=False,
        )

    return FileMemoryResolution(
        storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
        use_configured_path=storage_paths_match(
            original_storage_path,
            resolved_storage_path,
        ),
    )


def storage_paths_match(original_storage_path: Path, resolved_storage_path: Path) -> bool:
    """Return whether two storage roots resolve to the same canonical path."""
    return original_storage_path.expanduser().resolve() == resolved_storage_path.expanduser().resolve()


def resolve_file_memory_resolution(
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str | None = None,
    original_storage_path: Path | None = None,
    preserve_resolved_storage_path: bool = False,
) -> FileMemoryResolution:
    """Resolve file-memory storage settings for one caller context."""
    resolved_storage_path = storage_path
    base_storage_path = original_storage_path or storage_path
    agent_config = config.agents.get(agent_name) if agent_name is not None else None
    if agent_name is not None:
        if agent_config is not None and agent_config.private is not None:
            resolved_storage_path = resolve_agent_private_state_storage_path(
                agent_name,
                config,
                base_storage_path=base_storage_path,
                execution_identity=get_tool_execution_identity(),
            )
        else:
            resolved_storage_path = resolve_agent_state_storage_path(
                agent_name=agent_name,
                base_storage_path=base_storage_path,
            )
    resolution = file_memory_resolution_from_paths(
        original_storage_path=base_storage_path,
        resolved_storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )
    if agent_name is None:
        return resolution

    if agent_config is None or agent_config.memory_file_path is None:
        return resolution

    agent_memory_scope_path = resolve_agent_owned_path(
        agent_config.memory_file_path,
        agent_name=agent_name,
        base_storage_path=storage_path,
    )
    return FileMemoryResolution(
        storage_path=resolution.storage_path,
        runtime_paths=runtime_paths,
        use_configured_path=resolution.use_configured_path,
        agent_memory_scope_path=agent_memory_scope_path,
    )
