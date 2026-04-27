"""Runtime-derived entity resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import mindroom.matrix.rooms as matrix_rooms
from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_naming import agent_username_localpart, extract_server_name_from_homeserver

if TYPE_CHECKING:
    from mindroom.config.agent import AgentConfig, TeamConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def configured_bot_usernames_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return bot username localparts configured for one Matrix room."""
    configured_bots: set[str] = set()

    for agent_name, agent_config in config.agents.items():
        resolved_rooms = set(matrix_rooms.resolve_room_aliases(agent_config.rooms, runtime_paths))
        if room_id in resolved_rooms:
            configured_bots.add(agent_username_localpart(agent_name, runtime_paths))

    for team_name, team_config in config.teams.items():
        resolved_rooms = set(matrix_rooms.resolve_room_aliases(team_config.rooms, runtime_paths))
        if room_id in resolved_rooms:
            configured_bots.add(agent_username_localpart(team_name, runtime_paths))

    if configured_bots:
        configured_bots.add(agent_username_localpart(ROUTER_AGENT_NAME, runtime_paths))

    return configured_bots


def matrix_domain(runtime_paths: RuntimePaths) -> str:
    """Return the Matrix domain for one explicit runtime context."""
    homeserver = runtime_matrix_homeserver(runtime_paths)
    return extract_server_name_from_homeserver(homeserver, runtime_paths)


def entity_matrix_ids(config: Config, runtime_paths: RuntimePaths) -> dict[str, MatrixID]:
    """Return Matrix IDs for configured agents, teams, and the router."""
    domain = matrix_domain(runtime_paths)
    mapping: dict[str, MatrixID] = {
        agent_name: MatrixID.from_agent(agent_name, domain, runtime_paths) for agent_name in config.agents
    }
    mapping[ROUTER_AGENT_NAME] = MatrixID.from_agent(ROUTER_AGENT_NAME, domain, runtime_paths)
    mapping.update(
        {team_name: MatrixID.from_agent(team_name, domain, runtime_paths) for team_name in config.teams},
    )
    return mapping


def mindroom_user_id(config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the configured internal user's full Matrix ID."""
    if config.mindroom_user is None:
        return None
    return MatrixID.from_username(config.mindroom_user.username, matrix_domain(runtime_paths)).full_id


def resolve_agent_thread_mode(
    agent_config: AgentConfig,
    room_id: str | None,
    runtime_paths: RuntimePaths,
) -> Literal["thread", "room"]:
    """Resolve one agent's effective thread mode for an optional room context."""
    default_mode = agent_config.thread_mode
    if room_id is None or not agent_config.room_thread_modes:
        return default_mode

    overrides = agent_config.room_thread_modes
    direct_mode = overrides.get(room_id)
    if direct_mode is not None:
        return direct_mode

    room_alias = matrix_rooms.get_room_alias_from_id(room_id, runtime_paths)
    if room_alias:
        alias_mode = overrides.get(room_alias)
        if alias_mode is not None:
            return alias_mode

    for override_key, resolved_room_id in zip(
        overrides,
        matrix_rooms.resolve_room_aliases(list(overrides), runtime_paths),
        strict=False,
    ):
        if resolved_room_id == room_id:
            return overrides[override_key]

    return default_mode


def router_agents_for_room(
    agents: dict[str, AgentConfig],
    teams: dict[str, TeamConfig],
    room_id: str | None,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return agents relevant for router mode resolution in one room context."""
    if room_id is None:
        return set(agents)

    router_agents: set[str] = set()
    for agent_name, agent_cfg in agents.items():
        if room_id in set(matrix_rooms.resolve_room_aliases(agent_cfg.rooms, runtime_paths)):
            router_agents.add(agent_name)
    for team_cfg in teams.values():
        if room_id not in set(matrix_rooms.resolve_room_aliases(team_cfg.rooms, runtime_paths)):
            continue
        router_agents.update(agent_name for agent_name in team_cfg.agents if agent_name in agents)
    return router_agents or set(agents)


def effective_entity_model_name(
    config: Config,
    entity_name: str,
    room_id: str | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the effective model for one entity in one room context."""
    if entity_name not in config.agents and entity_name not in config.teams and entity_name != ROUTER_AGENT_NAME:
        return "default"
    if room_id is not None:
        room_alias = matrix_rooms.get_room_alias_from_id(room_id, runtime_paths)
        if room_alias and room_alias in config.room_models:
            return config.room_models[room_alias]
    return config.get_entity_model_name(entity_name)
