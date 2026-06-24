"""Runtime-derived entity resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.matrix import state as matrix_state
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id
from mindroom.matrix_identifiers import (
    extract_server_name_from_homeserver,
    room_alias_identifier_candidates,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.agent import AgentConfig, TeamConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_INTERNAL_USER_ENTITY_NAME = "user"


class MissingManagedEntityAccountError(RuntimeError):
    """Raised when runtime entity identity is requested before account preparation."""


class DuplicateManagedEntityIdentityError(RuntimeError):
    """Raised when persisted managed Matrix accounts are ambiguous."""


def configured_bot_user_ids_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_aliases: Iterable[str] = (),
) -> set[str]:
    """Return bot Matrix user IDs configured for one Matrix room."""
    configured_names = configured_routable_entity_names_for_room(
        config,
        room_id,
        runtime_paths,
        room_aliases=room_aliases,
    )
    if not configured_names:
        return set()
    config_ids = entity_identity_registry(config, runtime_paths).current_ids
    configured_bots = {config_ids[entity_name].full_id for entity_name in configured_names}

    if configured_bots:
        configured_bots.add(config_ids[ROUTER_AGENT_NAME].full_id)

    return configured_bots


def is_configured_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_aliases: Iterable[str] = (),
) -> bool:
    """Return whether one Matrix room is present in the configured room set."""
    room_identifiers = _room_reference_identifiers(room_id, runtime_paths, room_aliases=room_aliases)
    return _room_refs_match(list(config.get_all_configured_rooms()), room_identifiers, runtime_paths)


def configured_routable_entity_names_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_aliases: Iterable[str] = (),
) -> list[str]:
    """Return non-router agent and team names statically configured for one room."""
    room_identifiers = _room_reference_identifiers(room_id, runtime_paths, room_aliases=room_aliases)
    configured_names: list[str] = []

    for agent_name, agent_config in config.agents.items():
        if agent_name == ROUTER_AGENT_NAME:
            continue
        if _room_refs_match(agent_config.rooms, room_identifiers, runtime_paths):
            configured_names.append(agent_name)

    for team_name, team_config in config.teams.items():
        if _room_refs_match(team_config.rooms, room_identifiers, runtime_paths):
            configured_names.append(team_name)

    return configured_names


def _room_refs_match(
    configured_room_refs: list[str],
    room_identifiers: set[str],
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether authored room refs match any known identifier for a live room."""
    if room_identifiers.intersection(configured_room_refs):
        return True
    resolved_refs = matrix_state.resolve_room_aliases(configured_room_refs, runtime_paths)
    return bool(room_identifiers.intersection(resolved_refs))


def _room_reference_identifiers(
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_aliases: Iterable[str] = (),
) -> set[str]:
    """Return room ID, alias, and managed key identifiers for one live room."""
    identifiers: list[str] = []

    def add(identifier: str | None) -> None:
        if identifier:
            identifiers.append(identifier)

    add(room_id)
    if room_id.startswith("#"):
        _add_room_alias_identifiers(identifiers, room_id, runtime_paths)
    for room_alias in room_aliases:
        _add_room_alias_identifiers(identifiers, room_alias, runtime_paths)

    matched_identifiers = set(identifiers)
    state = matrix_state.matrix_state_for_runtime(runtime_paths)
    for room_key, room in state.rooms.items():
        persisted_identifiers = {room_key, room.room_id, room.alias}
        if not matched_identifiers.intersection(persisted_identifiers):
            continue
        add(room_key)
        add(room.room_id)
        _add_room_alias_identifiers(identifiers, room.alias, runtime_paths)
        matched_identifiers = set(identifiers)

    return set(identifiers)


def _add_room_alias_identifiers(
    identifiers: list[str],
    room_alias: str,
    runtime_paths: RuntimePaths,
) -> None:
    identifiers.extend(room_alias_identifier_candidates(room_alias, runtime_paths))


def configured_routable_entity_ids_for_room(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
    *,
    room_aliases: Iterable[str] = (),
) -> list[MatrixID]:
    """Return non-router agent and team IDs statically configured for one room."""
    configured_names = configured_routable_entity_names_for_room(
        config,
        room_id,
        runtime_paths,
        room_aliases=room_aliases,
    )
    config_ids = entity_identity_registry(config, runtime_paths).current_ids
    return [config_ids[name] for name in configured_names]


def managed_entity_power_user_ids_for_room(
    room_key: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Return managed bot Matrix IDs that should receive create-time room power."""
    config_ids = entity_identity_registry(config, runtime_paths).current_ids
    entity_ids = [config_ids[ROUTER_AGENT_NAME].full_id]

    for agent_name, agent_config in config.agents.items():
        if room_key in agent_config.rooms:
            entity_ids.append(config_ids[agent_name].full_id)
    for team_name, team_config in config.teams.items():
        if room_key in team_config.rooms:
            entity_ids.append(config_ids[team_name].full_id)
    return entity_ids


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
    _validate_internal_user_id_is_unique(config, runtime_paths, current_ids)
    return EntityIdentityRegistry(current_ids=current_ids)


def current_entity_id(entity_name: str, runtime_paths: RuntimePaths) -> MatrixID:
    """Return one persisted Matrix ID without resolving unrelated configured entities."""
    return _persisted_entity_matrix_id(entity_name, _matrix_domain(runtime_paths), runtime_paths)


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
        raise MissingManagedEntityAccountError(msg)
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
        raise DuplicateManagedEntityIdentityError(msg)


def _validate_internal_user_id_is_unique(
    config: Config,
    runtime_paths: RuntimePaths,
    current_ids: dict[str, MatrixID],
) -> None:
    internal_user_id = mindroom_user_id(config, runtime_paths)
    if internal_user_id is None:
        return
    for entity_name, matrix_id in current_ids.items():
        if matrix_id.full_id != internal_user_id:
            continue
        msg = (
            "MindRoom internal user Matrix ID must not match a configured entity Matrix ID: "
            f"{internal_user_id} is also used by {entity_name!r}"
        )
        raise DuplicateManagedEntityIdentityError(msg)


def mindroom_user_id(config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the configured internal user's full Matrix ID."""
    if config.mindroom_user is None:
        return None
    domain = _matrix_domain(runtime_paths)
    return managed_account_user_id(
        managed_account_key(_INTERNAL_USER_ENTITY_NAME),
        domain,
        runtime_paths,
    )


def current_internal_sender_ids(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return current runtime-owned Matrix IDs trusted as internal senders."""
    sender_ids = set(entity_identity_registry(config, runtime_paths).internal_sender_ids)
    if internal_user_id := mindroom_user_id(config, runtime_paths):
        sender_ids.add(internal_user_id)
    return frozenset(sender_ids)


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
    if room_override := resolve_room_scoped_model_override(config.room_models, room_id, runtime_paths):
        return room_override
    return config.get_entity_model_name(entity_name)


def resolve_room_scoped_model_override(
    overrides: Mapping[str, str],
    room_id: str | None,
    runtime_paths: RuntimePaths,
    *,
    allow_raw_room_id: bool = False,
) -> str | None:
    """Return a model override keyed by persisted room key or alias."""
    if room_id is None or not overrides:
        return None
    if allow_raw_room_id and room_id in overrides:
        return overrides[room_id]
    room_alias = matrix_state.get_room_alias_from_id(room_id, runtime_paths)
    if room_alias and room_alias in overrides:
        return overrides[room_alias]
    for room in matrix_state.matrix_state_for_runtime(runtime_paths).rooms.values():
        if room.room_id == room_id and room.alias in overrides:
            return overrides[room.alias]
    return None
