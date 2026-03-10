"""Configuration diffing and reload planning for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConfigUpdatePlan:
    """Computed impact of one config reload."""

    new_config: Config
    all_new_entities: set[str]
    entities_to_restart: set[str]
    new_entities: set[str]
    removed_entities: set[str]
    mindroom_user_changed: bool
    matrix_room_access_changed: bool
    matrix_space_changed: bool
    authorization_changed: bool

    @property
    def has_entity_changes(self) -> bool:
        """Return whether any bots must be created, restarted, or removed."""
        return bool(self.entities_to_restart or self.new_entities or self.removed_entities)

    @property
    def only_support_service_changes(self) -> bool:
        """Return whether only non-bot support services changed."""
        return not (
            self.has_entity_changes
            or self.mindroom_user_changed
            or self.matrix_room_access_changed
            or self.matrix_space_changed
            or self.authorization_changed
        )


def _config_entries_differ(old_entry: BaseModel | None, new_entry: BaseModel | None) -> bool:
    """Compare optional config models using the same shape as persisted YAML."""
    if old_entry is None or new_entry is None:
        return old_entry != new_entry
    return old_entry.model_dump(exclude_none=True) != new_entry.model_dump(exclude_none=True)


def _identify_entities_to_restart(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Identify entities that need restarting due to config changes."""
    agents_to_restart = _get_changed_agents(config, new_config, agent_bots)
    teams_to_restart = _get_changed_teams(config, new_config, agent_bots)

    entities_to_restart = agents_to_restart | teams_to_restart

    if _router_needs_restart(config, new_config):
        entities_to_restart.add("router")

    return entities_to_restart


def _get_changed_agents(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Return agent names whose config or culture changed."""
    if not config:
        return set()

    changed = set()
    all_agents = set(config.agents.keys()) | set(new_config.agents.keys())

    for agent_name in all_agents:
        old_agent = config.agents.get(agent_name)
        new_agent = new_config.agents.get(agent_name)

        agents_differ = _config_entries_differ(old_agent, new_agent)
        old_culture = _culture_signature_for_agent(agent_name, config) if old_agent else None
        new_culture = _culture_signature_for_agent(agent_name, new_config) if new_agent else None
        culture_differ = old_culture != new_culture

        if (agents_differ or culture_differ) and (agent_name in agent_bots or new_agent is not None):
            if old_agent and new_agent:
                if agents_differ:
                    logger.debug(f"Agent {agent_name} configuration changed, will restart")
                else:
                    logger.debug(f"Agent {agent_name} culture assignment changed, will restart")
            elif new_agent:
                logger.info(f"Agent {agent_name} is new, will start")
            else:
                logger.info(f"Agent {agent_name} was removed, will stop")
            changed.add(agent_name)

    return changed


def _culture_signature_for_agent(agent_name: str, config: Config) -> tuple[str, str, str] | None:
    """Return the relevant culture tuple used for restart decisions."""
    assignment = config.get_agent_culture(agent_name)
    if assignment is None:
        return None
    culture_name, culture_config = assignment
    return (culture_name, culture_config.mode, culture_config.description)


def _get_changed_teams(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Return team names whose config changed."""
    if not config:
        return set()

    changed = set()
    all_teams = set(config.teams.keys()) | set(new_config.teams.keys())

    for team_name in all_teams:
        old_team = config.teams.get(team_name)
        new_team = new_config.teams.get(team_name)
        teams_differ = _config_entries_differ(old_team, new_team)

        if teams_differ and (team_name in agent_bots or new_team is not None):
            changed.add(team_name)

    return changed


def _router_needs_restart(config: Config | None, new_config: Config) -> bool:
    """Check if router needs restart due to room changes."""
    if not config:
        return False

    old_rooms = config.get_all_configured_rooms()
    new_rooms = new_config.get_all_configured_rooms()
    return old_rooms != new_rooms


def build_config_update_plan(
    *,
    current_config: Config,
    new_config: Config,
    configured_entities: set[str],
    existing_entities: set[str],
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> ConfigUpdatePlan:
    """Compute the effect of reloading config for the current runtime state."""
    entities_to_restart = _identify_entities_to_restart(
        current_config,
        new_config,
        agent_bots,
    )
    new_entities = configured_entities - existing_entities - entities_to_restart

    return ConfigUpdatePlan(
        new_config=new_config,
        all_new_entities=configured_entities,
        entities_to_restart=entities_to_restart,
        new_entities=new_entities,
        removed_entities=existing_entities - configured_entities,
        mindroom_user_changed=current_config.mindroom_user != new_config.mindroom_user,
        matrix_room_access_changed=current_config.matrix_room_access != new_config.matrix_room_access,
        matrix_space_changed=current_config.matrix_space != new_config.matrix_space,
        authorization_changed=current_config.authorization != new_config.authorization,
    )
