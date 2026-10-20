"""Memory scope and storage-root policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.memory_scope_ids import agent_name_from_scope_user_id, agent_scope_user_id
from mindroom.runtime_resolution import resolve_agent_runtime

from ._shared import FileMemoryResolution

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryBackend
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _agent_uses_file_memory_backend(config: Config, agent_name: str) -> bool:
    return config.resolve_entity(agent_name).memory_backend == "file"


def _agent_uses_disabled_memory_backend(config: Config, agent_name: str) -> bool:
    return config.resolve_entity(agent_name).memory_backend == "none"


def caller_uses_file_memory_backend(config: Config, caller_context: str | list[str]) -> bool:
    """Return whether the caller context resolves to file-backed memory."""
    if isinstance(caller_context, str):
        return _agent_uses_file_memory_backend(config, caller_context)
    return _team_uses_file_memory_backend(config, caller_context)


def caller_uses_disabled_memory_backend(config: Config, caller_context: str | list[str]) -> bool:
    """Return whether the caller context resolves to disabled memory."""
    if isinstance(caller_context, str):
        return _agent_uses_disabled_memory_backend(config, caller_context)
    return _team_uses_disabled_memory_backend(config, caller_context)


def _team_uses_file_memory_backend(config: Config, agent_names: list[str]) -> bool:
    """Return whether all team members resolve to file-backed memory."""
    config.assert_team_agents_supported(agent_names)
    return all(_agent_uses_file_memory_backend(config, agent_name) for agent_name in agent_names)


def _team_uses_disabled_memory_backend(config: Config, agent_names: list[str]) -> bool:
    """Return whether all team members resolve to disabled memory."""
    config.assert_team_agents_supported(agent_names)
    return all(_agent_uses_disabled_memory_backend(config, agent_name) for agent_name in agent_names)


@dataclass(frozen=True)
class TeamMemoryBackendMembers:
    """Team members grouped by their effective memory backend."""

    file: list[str]
    mem0: list[str]
    none: list[str]

    @property
    def partitioned(self) -> bool:
        """Return whether team memory must be split across member backends."""
        return bool(self.none or (self.file and self.mem0))


def team_members_by_memory_backend(config: Config, agent_names: list[str]) -> TeamMemoryBackendMembers:
    """Group team members by their effective memory backend."""
    config.assert_team_agents_supported(agent_names)
    members: dict[MemoryBackend, list[str]] = {"file": [], "mem0": [], "none": []}
    for agent_name in agent_names:
        members[config.resolve_entity(agent_name).memory_backend].append(agent_name)
    return TeamMemoryBackendMembers(file=members["file"], mem0=members["mem0"], none=members["none"])


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


def team_members_from_scope_user_id(scope_user_id: str, config: Config) -> list[str] | None:
    """Resolve team member names from a team-scoped memory user ID."""
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
    *,
    target_agent_names: list[str] | None = None,
) -> list[Path]:
    """Return the canonical storage roots for one memory scope.

    For a team scope, ``target_agent_names`` restricts the roots to those members.
    """
    if (team_members := team_members_from_scope_user_id(scope_user_id, config)) is not None:
        storage_members = team_members
        if target_agent_names is not None:
            unsupported_members = [agent_name for agent_name in target_agent_names if agent_name not in team_members]
            if unsupported_members:
                msg = f"Target agents are not members of {scope_user_id}: {unsupported_members}"
                raise ValueError(msg)
            storage_members = target_agent_names
        return effective_storage_paths_for_context(
            storage_members,
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


def _file_memory_resolution_from_paths(
    *,
    original_storage_path: Path,
    resolved_storage_path: Path,
    runtime_paths: RuntimePaths,
) -> FileMemoryResolution:
    """Build file-memory resolution settings from original and resolved roots."""
    return FileMemoryResolution(
        storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
        use_configured_path=_storage_paths_match(
            original_storage_path,
            resolved_storage_path,
        ),
    )


def _storage_paths_match(original_storage_path: Path, resolved_storage_path: Path) -> bool:
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
    resolution = _file_memory_resolution_from_paths(
        original_storage_path=base_storage_path,
        resolved_storage_path=resolved_storage_path,
        runtime_paths=runtime_paths,
    )
    if agent_name is None or agent_memory_scope_path is not None:
        return FileMemoryResolution(
            storage_path=resolution.storage_path,
            runtime_paths=runtime_paths,
            use_configured_path=resolution.use_configured_path,
            agent_memory_scope_path=agent_memory_scope_path,
        )
    return resolution
