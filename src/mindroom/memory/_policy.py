"""Memory scope and storage-root policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.runtime_resolution import resolve_agent_runtime

from ._shared import FileMemoryResolution

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


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
    config.assert_team_agents_supported(agent_names)
    return all(use_file_memory_backend(config, agent_name=agent_name) for agent_name in agent_names)


def effective_storage_paths_for_context(
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> list[Path]:
    """Return the distinct storage roots affected by the caller context."""
    if isinstance(caller_context, str):
        return [_effective_storage_path_for_agent(caller_context, config, runtime_paths, execution_identity)]

    config.assert_team_agents_supported(caller_context)
    effective_paths: list[Path] = []
    for agent_name in caller_context:
        effective_path = _effective_storage_path_for_agent(agent_name, config, runtime_paths, execution_identity)
        if effective_path not in effective_paths:
            effective_paths.append(effective_path)
    return effective_paths or [storage_path]


def _effective_storage_path_for_agent(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> Path:
    return resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ).state_root


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
    team_ids: list[str] = []
    for team_name, team_config in config.teams.items():
        config.assert_team_agents_supported(team_config.agents, team_name=team_name)
        if agent_name in team_config.agents:
            team_ids.append(build_team_user_id(team_config.agents))
    return team_ids


def _team_members_from_scope_user_id(scope_user_id: str, config: Config) -> list[str] | None:
    if not scope_user_id.startswith("team_"):
        return None
    if config.teams:
        for team_config in config.teams.values():
            if build_team_user_id(team_config.agents) == scope_user_id:
                return list(team_config.agents)
    members = scope_user_id[len("team_") :].split("+")
    return members or None


def storage_paths_for_scope_user_id(
    scope_user_id: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> list[Path]:
    """Return the canonical storage roots for one memory scope."""
    if (team_members := _team_members_from_scope_user_id(scope_user_id, config)) is not None:
        return effective_storage_paths_for_context(
            team_members,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
    if (agent_name := agent_name_from_scope_user_id(scope_user_id)) is not None:
        return [_effective_storage_path_for_agent(agent_name, config, runtime_paths, execution_identity)]
    msg = f"Unsupported memory scope user_id: {scope_user_id}"
    raise ValueError(msg)


def get_allowed_memory_user_ids(caller_context: str | list[str], config: Config) -> set[str]:
    """Get all user_id scopes the caller is allowed to access."""
    if isinstance(caller_context, list):
        config.assert_team_agents_supported(caller_context)
        allowed_user_ids = {build_team_user_id(caller_context)}
        if config.memory.team_reads_member_memory:
            allowed_user_ids.update(agent_scope_user_id(agent_name) for agent_name in caller_context)
        return allowed_user_ids

    if caller_context not in config.agents:
        return set()

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
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    agent_name: str | None = None,
    original_storage_path: Path | None = None,
    preserve_resolved_storage_path: bool = False,
) -> FileMemoryResolution:
    """Resolve file-memory storage settings for one caller context."""
    resolved_storage_path = storage_path
    base_storage_path = original_storage_path or storage_path
    agent_memory_scope_path: Path | None = None
    if agent_name is not None:
        private_agent = config.get_agent(agent_name).private is not None
        agent_runtime = resolve_agent_runtime(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            create=private_agent,
        )
        resolved_storage_path = agent_runtime.state_root
        agent_memory_scope_path = agent_runtime.file_memory_root
    resolution = file_memory_resolution_from_paths(
        original_storage_path=base_storage_path,
        resolved_storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )
    if agent_name is None or agent_memory_scope_path is not None:
        return FileMemoryResolution(
            storage_path=resolution.storage_path,
            runtime_paths=runtime_paths,
            use_configured_path=resolution.use_configured_path,
            agent_memory_scope_path=agent_memory_scope_path,
        )
    return resolution
