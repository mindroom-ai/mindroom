"""Configuration diffing and reload planning for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from mindroom.tool_system.plugins import load_plugins

if TYPE_CHECKING:
    from pydantic import BaseModel

    from mindroom.orchestrator import MultiAgentOrchestrator

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConfigUpdatePlan:
    """Computed impact of one config reload."""

    current_config: Config
    new_config: Config
    all_new_entities: set[str]
    existing_entities: set[str]
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


async def _identify_entities_to_restart(
    config: Config | None,
    new_config: Config,
    agent_bots: dict[str, Any],
) -> set[str]:
    """Identify entities that need restarting due to config changes."""
    agents_to_restart = _get_changed_agents(config, new_config, agent_bots)
    teams_to_restart = _get_changed_teams(config, new_config, agent_bots)

    entities_to_restart = agents_to_restart | teams_to_restart

    if _router_needs_restart(config, new_config):
        entities_to_restart.add("router")

    return entities_to_restart


def _get_changed_agents(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
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


def _get_changed_teams(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
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


async def build_config_update_plan(orchestrator: MultiAgentOrchestrator, new_config: Config) -> ConfigUpdatePlan:
    """Compute the effect of reloading config for the current runtime state."""
    current_config = orchestrator.config
    if current_config is None:
        msg = "Cannot build update plan without an active config"
        raise RuntimeError(msg)

    entities_to_restart = await orchestrator.identify_entities_to_restart(
        current_config,
        new_config,
        orchestrator.agent_bots,
    )
    all_new_entities = set(orchestrator._configured_entity_names(new_config))
    existing_entities = set(orchestrator.agent_bots.keys())
    new_entities = all_new_entities - existing_entities - entities_to_restart

    return ConfigUpdatePlan(
        current_config=current_config,
        new_config=new_config,
        all_new_entities=all_new_entities,
        existing_entities=existing_entities,
        entities_to_restart=entities_to_restart,
        new_entities=new_entities,
        removed_entities=existing_entities - all_new_entities,
        mindroom_user_changed=current_config.mindroom_user != new_config.mindroom_user,
        matrix_room_access_changed=current_config.matrix_room_access != new_config.matrix_room_access,
        matrix_space_changed=current_config.matrix_space != new_config.matrix_space,
        authorization_changed=current_config.authorization != new_config.authorization,
    )


async def _load_initial_config(
    orchestrator: MultiAgentOrchestrator,
    new_config: Config,
) -> bool:
    """Handle config loading before the runtime has an active config."""
    await orchestrator._prepare_user_account(new_config, update_runtime_state=not orchestrator.running)
    orchestrator.config = new_config
    await orchestrator._sync_runtime_support_services(new_config, start_watcher=orchestrator.running)
    return False


async def _update_unchanged_bots(
    orchestrator: MultiAgentOrchestrator,
    plan: ConfigUpdatePlan,
) -> None:
    """Apply the new config to bots that do not require restart."""
    for entity_name, bot in orchestrator.agent_bots.items():
        if entity_name in plan.entities_to_restart:
            continue
        bot.config = plan.new_config
        bot.enable_streaming = plan.new_config.defaults.enable_streaming
        await bot._set_presence_with_model_info()
        logger.debug(f"Updated config for {entity_name}")


async def _remove_deleted_entities(
    orchestrator: MultiAgentOrchestrator,
    removed_entities: set[str],
) -> None:
    """Cancel, clean up, and unregister entities removed from config."""
    for entity_name in removed_entities:
        await orchestrator._cancel_bot_start_task(entity_name)
        await orchestrator.cancel_sync_task(entity_name, orchestrator._sync_tasks)

        bot = orchestrator.agent_bots.pop(entity_name, None)
        if bot is not None:
            await bot.cleanup()


async def _restart_changed_entities(
    orchestrator: MultiAgentOrchestrator,
    plan: ConfigUpdatePlan,
) -> tuple[set[str], list[str], list[str]]:
    """Restart or create entities affected by the config change."""
    if plan.entities_to_restart:
        for entity_name in plan.entities_to_restart:
            await orchestrator._cancel_bot_start_task(entity_name)
        await orchestrator.stop_entities(plan.entities_to_restart, orchestrator.agent_bots, orchestrator._sync_tasks)

    entities_to_recreate = plan.entities_to_restart & plan.all_new_entities
    changed_entities = entities_to_recreate | plan.new_entities
    start_results = await orchestrator._create_and_start_entities(
        changed_entities,
        plan.new_config,
        start_sync_tasks=True,
    )

    removed_restarted_entities = plan.entities_to_restart - plan.all_new_entities
    for entity_name in removed_restarted_entities:
        orchestrator.agent_bots.pop(entity_name, None)

    await _remove_deleted_entities(orchestrator, plan.removed_entities)
    return changed_entities, start_results.retryable_entities, start_results.permanently_failed_entities


async def _reconcile_post_update_rooms(
    orchestrator: MultiAgentOrchestrator,
    plan: ConfigUpdatePlan,
    changed_entities: set[str],
) -> None:
    """Reconcile rooms and memberships after entity/config updates."""
    bots_to_setup = orchestrator._running_bots_for_entities(changed_entities)
    if bots_to_setup or plan.mindroom_user_changed or plan.matrix_room_access_changed or plan.authorization_changed:
        await orchestrator._setup_rooms_and_memberships(bots_to_setup)
        return
    if plan.matrix_space_changed:
        room_ids = await orchestrator._ensure_rooms_exist()
        await orchestrator._ensure_root_space(room_ids)


async def update_config(self: MultiAgentOrchestrator) -> bool:
    """Update configuration and reconcile runtime entities."""
    new_config = Config.from_yaml()
    load_plugins(new_config)

    if not self.config:
        return await _load_initial_config(self, new_config)

    plan = await build_config_update_plan(self, new_config)

    if plan.mindroom_user_changed:
        await self._prepare_user_account(new_config, update_runtime_state=not self.running)

    self.config = new_config

    logger.info(
        f"Updating config. New authorization: {new_config.authorization.global_users}",
    )
    await _update_unchanged_bots(self, plan)

    if plan.only_support_service_changes:
        await self._sync_runtime_support_services(new_config, start_watcher=self.running)
        return False

    changed_entities, retryable_entities, permanently_failed_entities = await _restart_changed_entities(self, plan)
    await _reconcile_post_update_rooms(self, plan, changed_entities)

    for entity_name in retryable_entities:
        await self._schedule_bot_start_retry(entity_name)

    if permanently_failed_entities:
        logger.warning(
            "Configuration update left some bots disabled due to permanent startup errors",
            agent_names=permanently_failed_entities,
        )

    await self._sync_runtime_support_services(new_config, start_watcher=self.running)

    logger.info(
        f"Configuration update complete: {len(plan.entities_to_restart) + len(plan.new_entities)} bots affected",
    )
    return True
