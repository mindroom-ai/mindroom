"""Runtime-derived entity resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.matrix import state as matrix_state
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id
from mindroom.matrix_identifiers import extract_server_name_from_homeserver

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.agent import AgentConfig, TeamConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def configured_bot_usernames_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return bot username localparts configured for one Matrix room."""
    configured_names = configured_routable_entity_names_for_room(config, room_id, runtime_paths)
    config_ids = entity_matrix_ids(config, runtime_paths)
    configured_bots = {config_ids[entity_name].username for entity_name in configured_names}

    if configured_bots:
        configured_bots.add(config_ids[ROUTER_AGENT_NAME].username)

    return configured_bots


def configured_routable_entity_names_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Return non-router agent and team names statically configured for one room."""
    configured_names: list[str] = []

    for agent_name, agent_config in config.agents.items():
        if agent_name == ROUTER_AGENT_NAME:
            continue
        resolved_rooms = matrix_state.resolve_room_aliases(agent_config.rooms, runtime_paths)
        if room_id in resolved_rooms:
            configured_names.append(agent_name)

    for team_name, team_config in config.teams.items():
        resolved_rooms = matrix_state.resolve_room_aliases(team_config.rooms, runtime_paths)
        if room_id in resolved_rooms:
            configured_names.append(team_name)

    return configured_names


def configured_routable_entity_ids_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Return non-router agent and team IDs statically configured for one room."""
    configured_names = configured_routable_entity_names_for_room(config, room_id, runtime_paths)
    config_ids = entity_matrix_ids(config, runtime_paths)
    return [config_ids[name] for name in configured_names]


def matrix_domain(runtime_paths: RuntimePaths) -> str:
    """Return the Matrix domain for one explicit runtime context."""
    homeserver = runtime_matrix_homeserver(runtime_paths)
    return extract_server_name_from_homeserver(homeserver, runtime_paths)


def entity_matrix_ids(config: Config, runtime_paths: RuntimePaths) -> dict[str, MatrixID]:
    """Return Matrix IDs for configured agents, teams, and the router."""
    return _entity_matrix_id_map(config, runtime_paths, _entity_matrix_id)


@dataclass(frozen=True)
class EntityMatrixIdentity:
    """Current configured entity IDs plus stale bootstrap-ID predicates."""

    current_ids: dict[str, MatrixID]
    bootstrap_ids: dict[str, MatrixID]

    def current_entity_name_for_user_id(self, user_id: str, *, include_router: bool = True) -> str | None:
        """Return the configured entity currently represented by one Matrix user ID."""
        for entity_name, current_id in self.current_ids.items():
            if not include_router and entity_name == ROUTER_AGENT_NAME:
                continue
            if current_id.full_id == user_id:
                return entity_name
        return None

    def is_stale_localpart(self, entity_name: str, localpart: str) -> bool:
        """Return whether a localpart is this entity's old generated localpart."""
        generated_localpart = self.bootstrap_ids[entity_name].username
        if localpart.lower() != generated_localpart.lower():
            return False
        return self.current_ids[entity_name].username.lower() != localpart.lower()

    def is_stale_user_id(self, user_id: str) -> bool:
        """Return whether a user ID is an old generated ID for any configured entity."""
        return any(
            bootstrap_id.full_id == user_id and self.current_ids[entity_name].full_id != user_id
            for entity_name, bootstrap_id in self.bootstrap_ids.items()
        )


def entity_matrix_identity(config: Config, runtime_paths: RuntimePaths) -> EntityMatrixIdentity:
    """Return current entity IDs with generated-ID staleness checks."""
    return EntityMatrixIdentity(
        current_ids=entity_matrix_ids(config, runtime_paths),
        bootstrap_ids=_entity_matrix_id_map(config, runtime_paths, MatrixID.from_agent),
    )


def _entity_matrix_id_map(
    config: Config,
    runtime_paths: RuntimePaths,
    build_id: Callable[[str, str, RuntimePaths], MatrixID],
) -> dict[str, MatrixID]:
    domain = matrix_domain(runtime_paths)
    mapping = {agent_name: build_id(agent_name, domain, runtime_paths) for agent_name in config.agents}
    mapping[ROUTER_AGENT_NAME] = build_id(ROUTER_AGENT_NAME, domain, runtime_paths)
    mapping.update({team_name: build_id(team_name, domain, runtime_paths) for team_name in config.teams})
    return mapping


def _entity_matrix_id(entity_name: str, domain: str, runtime_paths: RuntimePaths) -> MatrixID:
    persisted_user_id = managed_account_user_id(managed_account_key(entity_name), domain, runtime_paths)
    if persisted_user_id is not None:
        return MatrixID.parse(persisted_user_id)
    return MatrixID.from_agent(entity_name, domain, runtime_paths)


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

    room_alias = matrix_state.get_room_alias_from_id(room_id, runtime_paths)
    if room_alias:
        alias_mode = overrides.get(room_alias)
        if alias_mode is not None:
            return alias_mode

    for override_key, resolved_room_id in zip(
        overrides,
        matrix_state.resolve_room_aliases(list(overrides), runtime_paths),
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
        if room_id in set(matrix_state.resolve_room_aliases(agent_cfg.rooms, runtime_paths)):
            router_agents.add(agent_name)
    for team_cfg in teams.values():
        if room_id not in set(matrix_state.resolve_room_aliases(team_cfg.rooms, runtime_paths)):
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
        room_alias = matrix_state.get_room_alias_from_id(room_id, runtime_paths)
        if room_alias and room_alias in config.room_models:
            return config.room_models[room_alias]
    return config.get_entity_model_name(entity_name)
