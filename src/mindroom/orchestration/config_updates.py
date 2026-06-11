"""Configuration diffing and reload planning for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.mcp.registry import mcp_tool_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)

_ENTITY_CONSTRUCTION_PROMPTS = frozenset(
    {
        "AGENT_IDENTITY_CONTEXT_TEMPLATE",
        "CODEX_DEFAULT_INSTRUCTIONS",
        "CONTEXT_TRUNCATION_MARKER_TEMPLATE",
        "DATETIME_CONTEXT_TEMPLATE",
        "DELEGATE_TOOLKIT_INSTRUCTIONS_TEMPLATE",
        "DYNAMIC_TOOLING_INSTRUCTION_TEMPLATE",
        "DYNAMIC_TOOLS_TOOLKIT_INSTRUCTIONS",
        "HIDDEN_TOOL_CALLS_PROMPT",
        "INTERACTIVE_QUESTION_PROMPT",
        "OPENAI_COMPAT_HISTORY_GUIDANCE",
        "OUTPUT_REDIRECT_PROMPT",
        "PERSONALITY_CONTEXT_SECTION_HEADING",
        "QUEUED_MESSAGE_NOTICE_TEXT",
        "SKILLS_TOOL_USAGE_PROMPT",
    },
)


@dataclass(frozen=True)
class ConfigUpdatePlan:
    """Computed impact of one config reload."""

    new_config: Config
    changed_mcp_servers: set[str]
    configured_entities: set[str]
    entities_to_restart: set[str]
    new_entities: set[str]
    removed_entities: set[str]
    mindroom_user_changed: bool
    matrix_room_access_changed: bool
    matrix_space_changed: bool
    authorization_changed: bool
    room_metadata_changed: bool = False
    added_entities: set[str] = field(default_factory=set)

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
            or self.room_metadata_changed
        )


def configured_entity_names(config: Config) -> list[str]:
    """Return configured entity names with the router first."""
    return [ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys()]


def plugin_change_paths(current_config: Config, new_config: Config) -> tuple[str, ...]:
    """Return plugin paths whose entry config changed across a reload."""
    old_entries = {entry.path: entry.model_dump(mode="python") for entry in current_config.plugins}
    new_entries = {entry.path: entry.model_dump(mode="python") for entry in new_config.plugins}
    changed_paths = {
        path for path in set(old_entries) | set(new_entries) if old_entries.get(path) != new_entries.get(path)
    }
    return tuple(sorted(changed_paths))


def _config_entries_differ(old_entry: BaseModel | None, new_entry: BaseModel | None) -> bool:
    """Compare optional config models using the same shape as persisted YAML."""
    if old_entry is None or new_entry is None:
        return old_entry != new_entry
    return old_entry.model_dump(exclude_none=True) != new_entry.model_dump(exclude_none=True)


def _identify_entities_to_restart(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
    changed_mcp_servers: set[str],
) -> set[str]:
    """Identify entities that need restarting due to config changes."""
    agents_to_restart = _get_changed_agents(config, new_config, agent_bots)
    teams_to_restart = _get_changed_teams(config, new_config, agent_bots)

    entities_to_restart = agents_to_restart | teams_to_restart
    if changed_mcp_servers:
        entities_to_restart |= _entities_referencing_mcp_servers(config, new_config, changed_mcp_servers)

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
                    logger.debug("agent_configuration_changed_restart_required", agent=agent_name)
                else:
                    logger.debug("agent_culture_changed_restart_required", agent=agent_name)
            elif new_agent:
                logger.info("new_agent_will_start", agent=agent_name)
            else:
                logger.info("removed_agent_will_stop", agent=agent_name)
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


def _room_metadata_changed(config: Config, new_config: Config) -> bool:
    """Return whether managed room metadata changed without implying bot reconstruction."""
    return config.rooms != new_config.rooms


def _changed_mcp_servers(
    config: Config | None,
    new_config: Config,
) -> set[str]:
    """Return MCP server ids whose config changed across a reload."""
    if config is None:
        return set(new_config.mcp_servers)
    all_server_ids = set(config.mcp_servers) | set(new_config.mcp_servers)
    return {
        server_id
        for server_id in all_server_ids
        if config.mcp_servers.get(server_id) != new_config.mcp_servers.get(server_id)
    }


def _entities_referencing_mcp_servers(
    config: Config | None,
    new_config: Config,
    changed_server_ids: set[str],
) -> set[str]:
    """Return entities that reference any changed MCP server tool."""
    tool_names = {mcp_tool_name(server_id) for server_id in changed_server_ids}
    old_entities = set() if config is None else config.get_entities_referencing_tools(tool_names)
    new_entities = new_config.get_entities_referencing_tools(tool_names)
    return old_entities | new_entities


def _changed_entity_construction_prompts(config: Config, new_config: Config) -> set[str]:
    """Return root prompt overrides that require entity reconstruction."""
    changed_prompt_names = {
        prompt_name
        for prompt_name in set(config.prompts) | set(new_config.prompts)
        if config.get_prompt(prompt_name) != new_config.get_prompt(prompt_name)
    }
    return changed_prompt_names & _ENTITY_CONSTRUCTION_PROMPTS


def _changed_entity_construction_defaults(config: Config, new_config: Config) -> set[str]:
    """Return defaults that require rebuilding agent and team entities."""
    if (
        config.defaults.tool_output_auto_save_threshold_bytes
        != new_config.defaults.tool_output_auto_save_threshold_bytes
    ):
        return {"tool_output_auto_save_threshold_bytes"}
    return set()


def build_config_update_plan(
    *,
    current_config: Config,
    new_config: Config,
    configured_entities: set[str],
    existing_entities: set[str],
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> ConfigUpdatePlan:
    """Compute the effect of reloading config for the current runtime state."""
    changed_mcp_servers = _changed_mcp_servers(current_config, new_config)
    entities_to_restart = _identify_entities_to_restart(
        current_config,
        new_config,
        agent_bots,
        changed_mcp_servers,
    )
    changed_entity_construction_prompts = _changed_entity_construction_prompts(current_config, new_config)
    if changed_entity_construction_prompts:
        prompt_affected_entities = existing_entities & configured_entities
        if prompt_affected_entities:
            logger.info(
                "entity_construction_prompts_changed_restart_required",
                prompts=sorted(changed_entity_construction_prompts),
                entities=sorted(prompt_affected_entities),
            )
        entities_to_restart |= prompt_affected_entities

    changed_entity_construction_defaults = _changed_entity_construction_defaults(current_config, new_config)
    if changed_entity_construction_defaults:
        default_affected_entities = existing_entities & (set(new_config.agents) | set(new_config.teams))
        if default_affected_entities:
            logger.info(
                "entity_construction_defaults_changed_restart_required",
                defaults=sorted(changed_entity_construction_defaults),
                entities=sorted(default_affected_entities),
            )
        entities_to_restart |= default_affected_entities

    added_entities = configured_entities - existing_entities
    new_entities = added_entities - entities_to_restart

    return ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=changed_mcp_servers,
        configured_entities=configured_entities,
        entities_to_restart=entities_to_restart,
        new_entities=new_entities,
        removed_entities=existing_entities - configured_entities,
        mindroom_user_changed=current_config.mindroom_user != new_config.mindroom_user,
        matrix_room_access_changed=current_config.matrix_room_access != new_config.matrix_room_access,
        matrix_space_changed=current_config.matrix_space != new_config.matrix_space,
        authorization_changed=current_config.authorization != new_config.authorization,
        room_metadata_changed=_room_metadata_changed(current_config, new_config),
        added_entities=added_entities,
    )
