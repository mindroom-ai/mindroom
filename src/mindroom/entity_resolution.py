"""Runtime-derived entity resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.matrix import state as matrix_state
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id
from mindroom.matrix_identifiers import extract_server_name_from_homeserver

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
    configured_names = configured_routable_entity_names_for_room(config, room_id, runtime_paths)
    if not configured_names:
        return set()
    config_ids = entity_identity_registry(config, runtime_paths).current_ids
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
    config_ids = entity_identity_registry(config, runtime_paths).current_ids
    return [config_ids[name] for name in configured_names]


def _matrix_domain(runtime_paths: RuntimePaths) -> str:
    """Return the Matrix domain for one explicit runtime context."""
    homeserver = runtime_matrix_homeserver(runtime_paths)
    return extract_server_name_from_homeserver(homeserver, runtime_paths)


@dataclass(frozen=True)
class EntityIdentityRegistry:
    """Current configured entity aliases mapped to actual persisted Matrix IDs."""

    current_ids: dict[str, MatrixID]

    def current_id(self, entity_name: str) -> MatrixID:
        """Return one configured entity's current persisted Matrix ID."""
        return self.current_ids[entity_name]

    def current_entity_name_for_user_id(self, user_id: str, *, include_router: bool = True) -> str | None:
        """Return the configured entity currently represented by one Matrix user ID."""
        for entity_name, current_id in self.current_ids.items():
            if not include_router and entity_name == ROUTER_AGENT_NAME:
                continue
            if current_id.full_id == user_id:
                return entity_name
        return None

    def is_managed_user_id(self, user_id: str, *, include_router: bool = True) -> bool:
        """Return whether a Matrix user ID belongs to a current configured entity."""
        return self.current_entity_name_for_user_id(user_id, include_router=include_router) is not None

    @property
    def internal_sender_ids(self) -> frozenset[str]:
        """Return current Matrix IDs trusted as managed internal senders."""
        return frozenset(matrix_id.full_id for matrix_id in self.current_ids.values())


def entity_identity_registry(config: Config, runtime_paths: RuntimePaths) -> EntityIdentityRegistry:
    """Return current persisted Matrix identities for configured runtime entities."""
    current_ids = _persisted_entity_id_map(config, runtime_paths)
    _validate_unique_entity_ids(current_ids)
    return EntityIdentityRegistry(current_ids=current_ids)


def _persisted_entity_id_map(config: Config, runtime_paths: RuntimePaths) -> dict[str, MatrixID]:
    domain = _matrix_domain(runtime_paths)
    return {
        entity_name: _persisted_entity_matrix_id(entity_name, domain, runtime_paths)
        for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]
    }


def _persisted_entity_matrix_id(entity_name: str, domain: str, runtime_paths: RuntimePaths) -> MatrixID:
    persisted_user_id = managed_account_user_id(managed_account_key(entity_name), domain, runtime_paths)
    if persisted_user_id is None:
        msg = f"Matrix account for configured entity {entity_name!r} has not been prepared"
        raise RuntimeError(msg)
    return MatrixID.parse(persisted_user_id)


def _validate_unique_entity_ids(current_ids: dict[str, MatrixID]) -> None:
    owners_by_user_id: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    for entity_name, matrix_id in current_ids.items():
        previous_owner = owners_by_user_id.get(matrix_id.full_id)
        if previous_owner is not None:
            duplicates.append((matrix_id.full_id, previous_owner, entity_name))
            continue
        owners_by_user_id[matrix_id.full_id] = entity_name
    if duplicates:
        formatted = ", ".join(
            f"{user_id} shared by {first_entity!r} and {second_entity!r}"
            for user_id, first_entity, second_entity in duplicates
        )
        msg = f"Configured entities must have unique Matrix IDs: {formatted}"
        raise RuntimeError(msg)


def mindroom_user_id(config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the configured internal user's full Matrix ID."""
    if config.mindroom_user is None:
        return None
    return MatrixID.from_username(config.mindroom_user.username, _matrix_domain(runtime_paths)).full_id


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
