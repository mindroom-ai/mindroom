"""Configuration diffing and reload planning for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.logging_config import get_logger
from mindroom.mcp.registry import mcp_tool_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.calls import LiveCallProfile
    from mindroom.config.main import Config

logger = get_logger(__name__)

_ENTITY_CONSTRUCTION_PROMPTS = frozenset(
    {
        "AGENT_IDENTITY_CONTEXT_TEMPLATE",
        "CODEX_DEFAULT_INSTRUCTIONS",
        "CONTEXT_CHUNK_OMITTED_MARKER_TEMPLATE",
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
        "WORKSPACE_SKILL_AUTHORING_PROMPT",
    },
)


# AgentConfig fields running bots read from the live config where they use them, so an
# edit reaches them without a restart; rooms reconcile memberships in place. Any other
# field restarts the agent: display_name is set as its Matrix profile name at login,
# accept_invites decides which invited rooms the bot loads when it is built, and private
# sets the storage and session identity of requester-private state. A tool edit still
# restarts the agent when it changes _agent_startup_tools().
_AGENT_LIVE_FIELDS = frozenset(
    {
        "access",
        "allow_self_config",
        "automations",
        "compaction",
        "compress_tool_results",
        "context_files",
        "credential_managers",
        "delegate_to",
        "file_access",
        "include_default_tools",
        "instructions",
        "knowledge_bases",
        "learning",
        "learning_mode",
        "markdown",
        "max_tool_calls_from_history",
        "max_tool_calls_per_turn",
        "memory_backend",
        "memory_search",
        "mid_turn",
        "minimal_instructions",
        "model",
        "num_history_messages",
        "num_history_runs",
        "participation",
        "role",
        "room_thread_modes",
        "rooms",
        "show_tool_calls",
        "skill_learning",
        "skills",
        "thread_exports",
        "thread_mode",
        "tools",
        "worker_scope",
        "worker_tools",
    },
)
# TeamConfig fields read live; display_name and accept_invites restart a team as they do an agent.
_TEAM_LIVE_FIELDS = frozenset(
    {
        "access",
        "agents",
        "compaction",
        "max_tool_calls_from_history",
        "max_tool_calls_per_turn",
        "mode",
        "model",
        "num_history_messages",
        "num_history_runs",
        "role",
        "rooms",
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
    room_access_changed: bool
    matrix_space_changed: bool
    authorization_changed: bool
    room_metadata_changed: bool = False
    reply_authorization_changed: bool = False
    added_entities: set[str] = field(default_factory=set)
    entities_to_reconcile_rooms: set[str] = field(default_factory=set)
    live_updated_entities: set[str] = field(default_factory=set)

    @property
    def requires_response_drain(self) -> bool:
        """Return whether publication must wait for in-flight responses.

        A plan that touches no entity and no reply-authorization input only
        replaces config that responses read live, so it can publish while they run.
        """
        return self.reply_authorization_changed or self._has_entity_changes

    @property
    def _has_entity_changes(self) -> bool:
        """Return whether any bots must be created, restarted, removed, or reconciled."""
        return bool(
            self.entities_to_restart or self.new_entities or self.removed_entities or self.entities_to_reconcile_rooms,
        )

    @property
    def only_support_service_changes(self) -> bool:
        """Return whether only non-bot support services changed."""
        return not (
            self._has_entity_changes
            or self.mindroom_user_changed
            or self.room_access_changed
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


def _changed_entry_fields(old_entry: BaseModel, new_entry: BaseModel) -> set[str]:
    """Return top-level fields that differ between two entries in persisted-YAML shape, ignoring rooms."""
    old_fields = old_entry.model_dump(exclude_none=True, exclude={"rooms"})
    new_fields = new_entry.model_dump(exclude_none=True, exclude={"rooms"})
    return {name for name in old_fields.keys() | new_fields.keys() if old_fields.get(name) != new_fields.get(name)}


def _restart_fields(
    old_entry: BaseModel,
    new_entry: BaseModel,
    live_fields: frozenset[str],
    bot: AgentBot | TeamBot | None,
) -> set[str]:
    """Return the changed entry fields that restart an entity's bot.

    A bot that is not running restarts on any change, so the edit retries its failed startup.
    """
    changed_fields = _changed_entry_fields(old_entry, new_entry)
    if changed_fields and bot is not None and not bot.running:
        return changed_fields
    return changed_fields - live_fields


def _agent_startup_tools(config: Config, agent_name: str) -> tuple[bool, frozenset[str]]:
    """Return the tool facts an agent bot acts on only when it starts.

    Startup registers the Desktop pairing receiver and holds the agent back while a
    required MCP server it uses is unavailable.
    """
    tool_names = set(config.resolve_entity(agent_name).available_tools)
    mcp_tool_names = {mcp_tool_name(server_id) for server_id in config.mcp_servers}
    return "desktop" in tool_names, frozenset(tool_names & mcp_tool_names)


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
    entities_to_restart |= _call_agents_to_restart(config, new_config, agent_bots)
    if changed_mcp_servers:
        entities_to_restart |= _entities_referencing_mcp_servers(config, new_config, changed_mcp_servers)

    return entities_to_restart


def _call_agents_to_restart(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Return call agents whose call setup changed or whose call in progress would keep stale config."""
    if config is None:
        return set()
    old_agents = set(config.calls.agents) if config.calls.enabled else set()
    new_agents = set(new_config.calls.agents) if new_config.calls.enabled else set()
    changed_agents = {
        agent_name
        for agent_name in old_agents | new_agents
        if _call_manager_signature(config, agent_name) != _call_manager_signature(new_config, agent_name)
    }
    if changed_agents:
        logger.info("call_manager_configuration_changed_restart_required", agents=sorted(changed_agents))
    # Idle call managers receive later config through CallManager.update_config(), but a call
    # in progress keeps the tools, prompt, and approval policy built from the config it joined with.
    agents_in_call = {
        agent_name
        for agent_name in old_agents - changed_agents
        if (bot := agent_bots.get(agent_name)) is not None and bot.active_call_requesters
    }
    if agents_in_call and config.authored_model_dump() != new_config.authored_model_dump():
        logger.info(
            "call_agent_configuration_changed_during_call_restart_required",
            agents=sorted(agents_in_call),
            reason="active call tooling captures the authored configuration snapshot",
        )
        changed_agents |= agents_in_call
    return changed_agents


def _call_manager_signature(config: Config, agent_name: str) -> object | None:
    """Return the call settings one agent's call manager is built from."""
    if not config.calls.enabled or agent_name not in config.calls.agents:
        return None
    profile = config.calls.resolve_agent_config(agent_name)
    model_name = None
    if profile.backend == "cascaded":
        model_name = profile.model
    elif profile.backend == "live":
        model_name = cast("LiveCallProfile", profile).agent_model
    model = config.models.get(model_name) if model_name is not None else None
    return (
        config.calls.livekit_service_url,
        config.calls.agents[agent_name],
        profile.model_dump(exclude_none=True),
        model.model_dump(exclude_none=True) if model is not None else None,
    )


def _get_changed_agents(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Return agents to restart: added, removed, or changed in what their bot reads only at startup."""
    if not config:
        return set()

    changed = set()
    all_agents = set(config.agents.keys()) | set(new_config.agents.keys())

    for agent_name in all_agents:
        old_agent = config.agents.get(agent_name)
        new_agent = new_config.agents.get(agent_name)

        if old_agent is None or new_agent is None:
            if new_agent is not None:
                logger.info("new_agent_will_start", agent=agent_name)
                changed.add(agent_name)
            elif agent_name in agent_bots:
                logger.info("removed_agent_will_stop", agent=agent_name)
                changed.add(agent_name)
            continue

        restart_fields = _restart_fields(old_agent, new_agent, _AGENT_LIVE_FIELDS, agent_bots.get(agent_name))
        if _agent_startup_tools(config, agent_name) != _agent_startup_tools(new_config, agent_name):
            restart_fields.add("tools")
        if restart_fields:
            logger.info("agent_configuration_changed_restart_required", agent=agent_name, fields=sorted(restart_fields))
            changed.add(agent_name)

    return changed


def _get_changed_teams(
    config: Config | None,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot | TeamBot],
) -> set[str]:
    """Return teams to restart: added, removed, or changed in what their bot reads only at startup."""
    if not config:
        return set()

    changed = set()
    all_teams = set(config.teams.keys()) | set(new_config.teams.keys())

    for team_name in all_teams:
        old_team = config.teams.get(team_name)
        new_team = new_config.teams.get(team_name)
        if old_team is None or new_team is None:
            if new_team is not None or team_name in agent_bots:
                changed.add(team_name)
            continue
        if _restart_fields(old_team, new_team, _TEAM_LIVE_FIELDS, agent_bots.get(team_name)):
            changed.add(team_name)

    return changed


def _entities_with_live_changes(config: Config, new_config: Config) -> set[str]:
    """Return agents and teams whose live-read fields changed."""
    sections = (
        (config.agents, new_config.agents, _AGENT_LIVE_FIELDS),
        (config.teams, new_config.teams, _TEAM_LIVE_FIELDS),
    )
    return {
        name
        for old_entries, new_entries, live_fields in sections
        for name in old_entries.keys() & new_entries.keys()
        if _changed_entry_fields(old_entries[name], new_entries[name]) & live_fields
    }


def _entities_with_room_changes(
    config: Config,
    new_config: Config,
    *,
    configured_entities: set[str],
    existing_entities: set[str],
) -> set[str]:
    """Return live entities whose desired Matrix room memberships changed."""
    return {
        entity_name
        for entity_name in configured_entities & existing_entities
        if set(get_rooms_for_entity(entity_name, config)) != set(get_rooms_for_entity(entity_name, new_config))
    }


def _reply_authorization_inputs_changed(config: Config, new_config: Config) -> bool:
    """Return whether config read by reply, invite, or trigger authorization changed.

    Per-entity rooms and invite policies are not listed because changing them
    already re-rooms or restarts that entity.
    """
    return (
        config.administrators != new_config.administrators
        or {name: agent.access for name, agent in config.agents.items()}
        != {name: agent.access for name, agent in new_config.agents.items()}
        or {name: team.access for name, team in config.teams.items()}
        != {name: team.access for name, team in new_config.teams.items()}
        or config.authorization != new_config.authorization
        or config.bot_accounts != new_config.bot_accounts
        or config.mindroom_user != new_config.mindroom_user
        or config.room_defaults != new_config.room_defaults
        or config.rooms != new_config.rooms
        or config.router.access != new_config.router.access
        or config.router.accept_invites != new_config.router.accept_invites
        or config.personal_rooms != new_config.personal_rooms
        or config.external_trigger_policy != new_config.external_trigger_policy
    )


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

    if current_config.matrix_sync != new_config.matrix_sync:
        # The sync transport is chosen when a bot's sync loop starts, so every
        # running entity must restart to pick up the new matrix_sync settings.
        sync_affected_entities = existing_entities & configured_entities
        if sync_affected_entities:
            logger.info(
                "matrix_sync_changed_restart_required",
                entities=sorted(sync_affected_entities),
            )
        entities_to_restart |= sync_affected_entities

    entities_to_reconcile_rooms = (
        _entities_with_room_changes(
            current_config,
            new_config,
            configured_entities=configured_entities,
            existing_entities=existing_entities,
        )
        - entities_to_restart
    )

    added_entities = configured_entities - existing_entities
    new_entities = added_entities - entities_to_restart
    live_updated_entities = (
        _entities_with_live_changes(current_config, new_config) & existing_entities
    ) - entities_to_restart

    return ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=changed_mcp_servers,
        configured_entities=configured_entities,
        entities_to_restart=entities_to_restart,
        new_entities=new_entities,
        removed_entities=existing_entities - configured_entities,
        mindroom_user_changed=current_config.mindroom_user != new_config.mindroom_user,
        room_access_changed=current_config.room_defaults != new_config.room_defaults
        or current_config.rooms != new_config.rooms,
        matrix_space_changed=current_config.matrix_space != new_config.matrix_space,
        authorization_changed=current_config.authorization != new_config.authorization,
        room_metadata_changed=_room_metadata_changed(current_config, new_config),
        reply_authorization_changed=_reply_authorization_inputs_changed(current_config, new_config),
        added_entities=added_entities,
        entities_to_reconcile_rooms=entities_to_reconcile_rooms,
        live_updated_entities=live_updated_entities,
    )
