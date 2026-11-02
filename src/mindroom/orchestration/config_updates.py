"""Configuration diffing and reload planning for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.access_policy import resolve_responder_access
from mindroom.agent_reply_membership import agent_reply_membership_policy_changed
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.logging_config import get_logger
from mindroom.mcp.registry import mcp_tool_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

    from pydantic import BaseModel

    from mindroom.bot import AgentBot
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
        "DELEGATE_BACKGROUND_JOB_INSTRUCTIONS",
        "DELEGATE_TOOLKIT_INSTRUCTIONS_TEMPLATE",
        "DYNAMIC_TOOLING_INSTRUCTION_TEMPLATE",
        "HIDDEN_TOOL_CALLS_PROMPT",
        "INTERACTIVE_QUESTION_PROMPT",
        "NATIVE_TOOL_SEARCH_INSTRUCTION_TEMPLATE",
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
        "cheap_model",
        "cold_cache",
        "compaction",
        "context_files",
        "created_agents_per_owner",
        "credential_managers",
        "delegate_to",
        "file_access",
        "include_default_tools",
        "instructions",
        "knowledge_bases",
        "knowledge_graph",
        "knowledge_graph_edits",
        "markdown",
        "max_tool_calls_from_history",
        "max_tool_calls_per_turn",
        "mcp_gateway_shared_credentials",
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
        "show_reasoning",
        "show_tool_calls",
        "skill_learning",
        "skills",
        "thread_exports",
        "thread_mode",
        "timezone",
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
    """Everything one config reload changes, decided in one place.

    ``restart``, ``start``, and ``remove`` are disjoint: an entity whose bot
    exists and stays configured may restart, a configured entity without a bot
    starts, and a bot whose entity is no longer configured is removed, never
    restarted, so its removal still finds the bot it must stop.
    """

    new_config: Config
    restart: frozenset[str] = frozenset()
    start: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()
    reconcile_rooms: frozenset[str] = frozenset()
    live_updated: frozenset[str] = frozenset()
    plugin_changes: tuple[str, ...] = ()
    mindroom_user_changed: bool = False
    room_access_changed: bool = False
    matrix_space_changed: bool = False
    server_emoji_changed: bool = False
    authorization_changed: bool = False
    # Any input reply, invite, or trigger authorization reads changed, so publication waits for replies.
    reply_authorization_changed: bool = False
    # A continuing agent's responder access changed, so background scripts it runs must be re-authorized.
    responder_access_changed: bool = False
    # Room-membership grant inputs changed, so every room-backed grant is revoked until the router refreshes.
    reply_membership_policy_changed: bool = False
    router_invite_policy_changed: bool = False

    @property
    def requires_response_drain(self) -> bool:
        """Return whether publication must wait for in-flight responses.

        A plan that touches no entity and no reply-authorization input only
        replaces config that responses read live, so it can publish while they run.
        """
        return self.reply_authorization_changed or bool(
            self.restart or self.start or self.remove or self.reconcile_rooms,
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
    if [entry.path for entry in current_config.plugins] != [entry.path for entry in new_config.plugins]:
        # Their order orders hooks of equal priority, so reordering plugins changes them all.
        changed_paths |= set(new_entries)
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
    bot: AgentBot | None,
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
    tool_names = set(config.resolve_agent(agent_name).tools.tool_names)
    mcp_tool_names = {mcp_tool_name(server_id) for server_id in config.mcp_servers}
    return "desktop" in tool_names, frozenset(tool_names & mcp_tool_names)


def _call_agents_to_restart(
    config: Config,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot],
) -> set[str]:
    """Return call agents whose call setup changed or whose call in progress would keep stale config."""
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
    if agents_in_call and _authored_outside_knowledge_graph(config) != _authored_outside_knowledge_graph(new_config):
        logger.info(
            "call_agent_configuration_changed_during_call_restart_required",
            agents=sorted(agents_in_call),
            reason="active call tooling captures the authored configuration snapshot",
        )
        changed_agents |= agents_in_call
    return changed_agents


def _authored_outside_knowledge_graph(config: Config) -> dict[str, object]:
    """Return the authored config without the knowledge_graph section.

    A call in progress keeps its graph lookups on the settings it joined with, as a reply in progress does, rather than
    ending for a change that only tunes or rebuilds the graphs.
    """
    authored = config.authored_model_dump()
    authored.pop("knowledge_graph", None)
    return authored


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
        # Budgets read prices at each spend refresh, so a price edit leaves the call manager as it is.
        model.model_dump(exclude_none=True, exclude={"pricing"}) if model is not None else None,
    )


def _get_changed_agents(
    config: Config,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot],
) -> set[str]:
    """Return kept agents changed in what their bot reads only at startup."""
    changed = set()
    for agent_name in config.agents.keys() & new_config.agents.keys():
        old_agent = config.agents[agent_name]
        new_agent = new_config.agents[agent_name]
        restart_fields = _restart_fields(old_agent, new_agent, _AGENT_LIVE_FIELDS, agent_bots.get(agent_name))
        if _agent_startup_tools(config, agent_name) != _agent_startup_tools(new_config, agent_name):
            restart_fields.add("tools")
        if restart_fields:
            logger.info("agent_configuration_changed_restart_required", agent=agent_name, fields=sorted(restart_fields))
            changed.add(agent_name)

    return changed


def _get_changed_teams(
    config: Config,
    new_config: Config,
    agent_bots: Mapping[str, AgentBot],
) -> set[str]:
    """Return kept teams changed in what their bot reads only at startup."""
    return {
        team_name
        for team_name in config.teams.keys() & new_config.teams.keys()
        if _restart_fields(
            config.teams[team_name],
            new_config.teams[team_name],
            _TEAM_LIVE_FIELDS,
            agent_bots.get(team_name),
        )
    }


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


def _responder_access_changed(config: Config, new_config: Config) -> bool:
    """Return whether a continuing agent's reply authority may change.

    Background scripts re-check their owner's authority only when this is true;
    a run whose bot is not running cannot be confirmed, so a broader trigger
    would end runs whose authority never changed.
    """
    if config.authorization != new_config.authorization or config.administrators != new_config.administrators:
        return True
    return any(
        resolve_responder_access(config, agent_name) != resolve_responder_access(new_config, agent_name)
        for agent_name in config.agents.keys() & new_config.agents.keys()
    )


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


def _changed_mcp_servers(config: Config, new_config: Config) -> frozenset[str]:
    """Return MCP server ids whose config changed across a reload."""
    all_server_ids = set(config.mcp_servers) | set(new_config.mcp_servers)
    return frozenset(
        server_id
        for server_id in all_server_ids
        if config.mcp_servers.get(server_id) != new_config.mcp_servers.get(server_id)
    )


def mcp_dependents(config: Config, server_ids: Iterable[str]) -> set[str]:
    """Return the entities whose tools include any of these MCP servers."""
    return config.get_entities_referencing_tools({mcp_tool_name(server_id) for server_id in server_ids})


def catalog_change_restarts(config: Config, server_ids: Iterable[str], existing_entities: AbstractSet[str]) -> set[str]:
    """Return the running entities to replace after these MCP servers changed their tool catalogs."""
    return mcp_dependents(config, server_ids) & existing_entities


def catalog_refresh_restarts(
    plan: ConfigUpdatePlan,
    catalog_changed: Iterable[str],
    running_entities: AbstractSet[str],
) -> tuple[set[str], list[str]]:
    """Decide what a reload does about MCP catalogs that changed while it connected their servers.

    Returns the running entities to replace in this reload, and the servers whose
    dependents restart later behind their own drain: a reload that drained
    replies replaces them now, one that publishes while replies run cannot.
    """
    server_ids = sorted(set(catalog_changed))
    if not server_ids:
        return set(), []
    if not plan.requires_response_drain:
        return set(), server_ids
    return catalog_change_restarts(plan.new_config, server_ids, running_entities), []


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
    bots: Mapping[str, AgentBot],
    plugin_changes: tuple[str, ...] = (),
) -> ConfigUpdatePlan:
    """Decide everything one reload changes for the current bots."""
    existing_entities = set(bots)
    configured_entities = set(configured_entity_names(new_config))
    kept_entities = existing_entities & configured_entities
    changed_mcp_servers = _changed_mcp_servers(current_config, new_config)

    restart = _get_changed_agents(current_config, new_config, bots) | _get_changed_teams(
        current_config,
        new_config,
        bots,
    )
    restart |= _call_agents_to_restart(current_config, new_config, bots)
    # A name that moved between agents and teams keeps its account, but its new entry is never compared with its old one.
    restart |= {name for name in kept_entities if (name in current_config.agents) != (name in new_config.agents)}
    # A bot the source config does not list is left over from a removal that failed part way, so it is rebuilt.
    restart |= kept_entities - set(configured_entity_names(current_config))
    if changed_mcp_servers:
        restart |= mcp_dependents(current_config, changed_mcp_servers) | mcp_dependents(
            new_config,
            changed_mcp_servers,
        )
    if plugin_changes:
        # Plugin code can register tools and hooks for any entity, so every bot rebuilds.
        logger.info("plugins_changed_restart_required", plugin_changes=list(plugin_changes))
        restart |= kept_entities

    changed_entity_construction_prompts = _changed_entity_construction_prompts(current_config, new_config)
    if changed_entity_construction_prompts and kept_entities:
        logger.info(
            "entity_construction_prompts_changed_restart_required",
            prompts=sorted(changed_entity_construction_prompts),
            entities=sorted(kept_entities),
        )
        restart |= kept_entities

    changed_entity_construction_defaults = _changed_entity_construction_defaults(current_config, new_config)
    default_affected_entities = kept_entities - {ROUTER_AGENT_NAME}
    if changed_entity_construction_defaults and default_affected_entities:
        logger.info(
            "entity_construction_defaults_changed_restart_required",
            defaults=sorted(changed_entity_construction_defaults),
            entities=sorted(default_affected_entities),
        )
        restart |= default_affected_entities

    if current_config.matrix_sync != new_config.matrix_sync and kept_entities:
        # The sync transport is chosen when a bot's sync loop starts, so every
        # running entity must restart to pick up the new matrix_sync settings.
        logger.info("matrix_sync_changed_restart_required", entities=sorted(kept_entities))
        restart |= kept_entities

    restart &= kept_entities
    start = configured_entities - existing_entities
    remove = existing_entities - configured_entities
    if start:
        logger.info("new_entities_will_start", entities=sorted(start))
    if remove:
        logger.info("removed_entities_will_stop", entities=sorted(remove))

    return ConfigUpdatePlan(
        new_config=new_config,
        restart=frozenset(restart),
        start=frozenset(start),
        remove=frozenset(remove),
        reconcile_rooms=frozenset(
            _entities_with_room_changes(
                current_config,
                new_config,
                configured_entities=configured_entities,
                existing_entities=existing_entities,
            )
            - restart,
        ),
        live_updated=frozenset((_entities_with_live_changes(current_config, new_config) & existing_entities) - restart),
        plugin_changes=plugin_changes,
        mindroom_user_changed=current_config.mindroom_user != new_config.mindroom_user,
        room_access_changed=current_config.room_defaults != new_config.room_defaults
        or current_config.rooms != new_config.rooms,
        matrix_space_changed=current_config.matrix_space != new_config.matrix_space,
        server_emoji_changed=current_config.server_emoji != new_config.server_emoji,
        authorization_changed=current_config.authorization != new_config.authorization,
        reply_authorization_changed=_reply_authorization_inputs_changed(current_config, new_config),
        responder_access_changed=_responder_access_changed(current_config, new_config),
        reply_membership_policy_changed=agent_reply_membership_policy_changed(current_config, new_config),
        router_invite_policy_changed=current_config.router.accept_invites != new_config.router.accept_invites,
    )
