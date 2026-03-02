"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

from mindroom.memory.auto_flush import MemoryAutoFlushWorker, auto_flush_enabled
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot

from .agents import get_rooms_for_entity
from .authorization import is_authorized_sender
from .bot import AgentBot, TeamBot, create_bot_for_entity
from .config.main import Config
from .constants import CONFIG_PATH, MATRIX_HOMESERVER, ROUTER_AGENT_NAME
from .credentials_sync import sync_env_to_credentials
from .file_watcher import watch_file
from .knowledge.manager import initialize_knowledge_managers, shutdown_knowledge_managers
from .logging_config import get_logger, setup_logging
from .matrix.client import get_joined_rooms, get_room_members, invite_to_room
from .matrix.identity import MatrixID, extract_server_name_from_homeserver
from .matrix.rooms import ensure_all_rooms_exist, ensure_user_in_rooms, load_rooms, resolve_room_aliases
from .matrix.state import MatrixState
from .matrix.users import (
    INTERNAL_USER_ACCOUNT_KEY,
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
    create_agent_user,
)

if TYPE_CHECKING:
    from .knowledge.manager import KnowledgeManager

logger = get_logger(__name__)


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _sync_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    knowledge_managers: dict[str, KnowledgeManager] = field(default_factory=dict, init=False)
    _memory_auto_flush_worker: MemoryAutoFlushWorker | None = field(default=None, init=False)
    _memory_auto_flush_task: asyncio.Task | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Store a canonical absolute storage path to survive runtime cwd changes."""
        self.storage_path = self.storage_path.expanduser().resolve()

    async def _stop_memory_auto_flush_worker(self) -> None:
        """Stop the background memory auto-flush worker if running."""
        worker = self._memory_auto_flush_worker
        task = self._memory_auto_flush_task
        self._memory_auto_flush_worker = None
        self._memory_auto_flush_task = None

        if worker is not None:
            worker.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _sync_memory_auto_flush_worker(self) -> None:
        """Start or stop background memory auto-flush worker based on current config."""
        config = self.config
        if config is None:
            await self._stop_memory_auto_flush_worker()
            return

        enabled = auto_flush_enabled(config)
        if not enabled:
            await self._stop_memory_auto_flush_worker()
            return

        task = self._memory_auto_flush_task
        if task is not None and not task.done():
            return

        worker = MemoryAutoFlushWorker(
            storage_path=self.storage_path,
            config_provider=lambda: self.config,
        )
        self._memory_auto_flush_worker = worker
        self._memory_auto_flush_task = asyncio.create_task(worker.run(), name="memory_auto_flush_worker")

    async def _ensure_user_account(self, config: Config) -> None:
        """Ensure a user account exists, creating one if necessary.

        This reuses the same create_agent_user function that agents use,
        treating the user as a special "agent" named "user".
        """
        # The user account is just another "agent" from the perspective of account management
        user_account = await create_agent_user(
            MATRIX_HOMESERVER,
            INTERNAL_USER_AGENT_NAME,
            config.mindroom_user.display_name,
            username=config.mindroom_user.username,
        )
        logger.info(f"User account ready: {user_account.user_id}")

    async def _configure_knowledge(self, config: Config, *, start_watcher: bool) -> None:
        """Initialize or reconfigure knowledge managers for the current config."""
        self.knowledge_managers = await initialize_knowledge_managers(
            config=config,
            storage_path=self.storage_path,
            start_watchers=start_watcher,
        )

    async def initialize(self) -> None:
        """Initialize all agent bots with self-management.

        Each agent is now responsible for ensuring its own user account and rooms.
        """
        logger.info("Initializing multi-agent system...")

        config = Config.from_yaml()
        load_plugins(config)

        # Ensure user account exists first
        await self._ensure_user_account(config)
        self.config = config
        await self._configure_knowledge(config, start_watcher=False)

        # Create bots for all configured entities
        # Make Router the first so that it can manage room invitations
        all_entities = [ROUTER_AGENT_NAME, *list(config.agents.keys()), *list(config.teams.keys())]

        for entity_name in all_entities:
            temp_user = _create_temp_user(entity_name, config)

            bot = create_bot_for_entity(entity_name, temp_user, config, self.storage_path)
            if bot is None:
                logger.warning(f"Could not create bot for {entity_name}")
                continue

            bot.orchestrator = self
            self.agent_bots[entity_name] = bot

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots."""
        if not self.agent_bots:
            await self.initialize()

        # Start each agent bot (this registers callbacks and logs in, but doesn't join rooms)
        start_tasks = [bot.try_start() for bot in self.agent_bots.values()]
        results = await asyncio.gather(*start_tasks)

        # Check for failures
        failed_agents = [bot.agent_name for bot, success in zip(self.agent_bots.values(), results) if not success]

        if len(failed_agents) == len(self.agent_bots):
            msg = "All agents failed to start - cannot proceed"
            raise RuntimeError(msg)
        if failed_agents:
            logger.warning(
                f"System starting in degraded mode. "
                f"Failed agents: {', '.join(failed_agents)} "
                f"({len(self.agent_bots) - len(failed_agents)}/{len(self.agent_bots)} operational)",
            )
        else:
            logger.info("All agent bots started successfully")

        self.running = True
        config = self.config
        if config is None:
            msg = "Configuration not loaded"
            raise RuntimeError(msg)
        await self._configure_knowledge(config, start_watcher=True)
        await self._sync_memory_auto_flush_worker()

        # Setup rooms and have all bots join them
        await self._setup_rooms_and_memberships(list(self.agent_bots.values()))

        # Create sync tasks for each bot with automatic restart on failure
        for entity_name, bot in self.agent_bots.items():
            # Create a task for each bot's sync loop with restart wrapper
            sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
            # Store the task reference for later cancellation
            self._sync_tasks[entity_name] = sync_task

        # Run all sync tasks
        await asyncio.gather(*tuple(self._sync_tasks.values()))

    async def update_config(self) -> bool:  # noqa: C901, PLR0912, PLR0915
        """Update configuration with simplified self-managing agents.

        Each agent handles its own user account creation and room management.

        Returns:
            True if any agents were updated, False otherwise.

        """
        new_config = Config.from_yaml()
        load_plugins(new_config)

        if not self.config:
            await self._ensure_user_account(new_config)
            self.config = new_config
            await self._configure_knowledge(new_config, start_watcher=self.running)
            return False

        current_config = self.config

        # Identify what changed - we can keep using the existing helper functions
        entities_to_restart = await _identify_entities_to_restart(current_config, new_config, self.agent_bots)
        mindroom_user_changed = current_config.mindroom_user != new_config.mindroom_user
        matrix_room_access_changed = current_config.matrix_room_access != new_config.matrix_room_access

        # Also check for new entities that didn't exist before
        all_new_entities = set(new_config.agents.keys()) | set(new_config.teams.keys()) | {ROUTER_AGENT_NAME}
        existing_entities = set(self.agent_bots.keys())
        new_entities = all_new_entities - existing_entities - entities_to_restart

        if mindroom_user_changed:
            await self._ensure_user_account(new_config)

        # Only apply the new config after all validation/account checks succeed.
        self.config = new_config
        await self._configure_knowledge(new_config, start_watcher=self.running)
        await self._sync_memory_auto_flush_worker()

        # Always update config for ALL existing bots (even those being restarted will get new config when recreated)
        logger.info(
            f"Updating config. New authorization: {new_config.authorization.global_users}",
        )
        for entity_name, bot in self.agent_bots.items():
            if entity_name not in entities_to_restart:
                bot.config = new_config
                bot.enable_streaming = new_config.defaults.enable_streaming
                await bot._set_presence_with_model_info()
                logger.debug(f"Updated config for {entity_name}")

        if (
            not entities_to_restart
            and not new_entities
            and not mindroom_user_changed
            and not matrix_room_access_changed
        ):
            # No entities to restart or create, we're done
            return False

        # Stop entities that need restarting
        if entities_to_restart:
            await _stop_entities(entities_to_restart, self.agent_bots, self._sync_tasks)

        # Recreate entities that need restarting using self-management
        for entity_name in entities_to_restart:
            if entity_name in all_new_entities:
                # Create temporary user object (will be updated by ensure_user_account)
                temp_user = _create_temp_user(entity_name, new_config)
                bot = create_bot_for_entity(entity_name, temp_user, new_config, self.storage_path)
                if bot:
                    bot.orchestrator = self
                    self.agent_bots[entity_name] = bot
                    # Agent handles its own setup (but doesn't join rooms yet)
                    if await bot.try_start():
                        # Start sync loop with automatic restart
                        sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
                        self._sync_tasks[entity_name] = sync_task
                    else:
                        # Remove the failed bot from our registry
                        del self.agent_bots[entity_name]
            # Entity was removed from config
            elif entity_name in self.agent_bots:
                del self.agent_bots[entity_name]

        # Create new entities
        for entity_name in new_entities:
            temp_user = _create_temp_user(entity_name, new_config)
            bot = create_bot_for_entity(entity_name, temp_user, new_config, self.storage_path)
            if bot:
                bot.orchestrator = self
                self.agent_bots[entity_name] = bot
                if await bot.try_start():
                    sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
                    self._sync_tasks[entity_name] = sync_task
                else:
                    # Remove the failed bot from our registry
                    del self.agent_bots[entity_name]

        # Handle removed entities (cleanup)
        removed_entities = existing_entities - all_new_entities
        for entity_name in removed_entities:
            # Cancel sync task first
            await _cancel_sync_task(entity_name, self._sync_tasks)

            if entity_name in self.agent_bots:
                bot = self.agent_bots[entity_name]
                await bot.cleanup()  # Agent handles its own cleanup
                del self.agent_bots[entity_name]

        # Setup rooms and have new/restarted bots join them
        bots_to_setup = [
            self.agent_bots[entity_name]
            for entity_name in entities_to_restart | new_entities
            if entity_name in self.agent_bots
        ]

        if bots_to_setup or mindroom_user_changed or matrix_room_access_changed:
            await self._setup_rooms_and_memberships(bots_to_setup)

        logger.info(f"Configuration update complete: {len(entities_to_restart) + len(new_entities)} bots affected")
        return True

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        await self._stop_memory_auto_flush_worker()
        await shutdown_knowledge_managers()
        self.knowledge_managers = {}

        # First cancel all sync tasks
        for entity_name in list(self._sync_tasks.keys()):
            await _cancel_sync_task(entity_name, self._sync_tasks)

        # Signal all bots to stop their sync loops
        for bot in self.agent_bots.values():
            bot.running = False

        # Now stop all bots
        stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")

    async def _setup_rooms_and_memberships(self, bots: list[AgentBot | TeamBot]) -> None:
        """Setup rooms and ensure all bots have correct memberships.

        This shared method handles the common room setup flow for both
        initial startup and configuration updates.

        Args:
            bots: Collection of bots to setup room memberships for

        """
        # Ensure all configured rooms exist (router creates them if needed)
        await self._ensure_rooms_exist()

        # After rooms exist, update each bot's room list to use room IDs instead of aliases
        config = self.config
        if config is None:
            msg = "Configuration not loaded"
            raise RuntimeError(msg)
        for bot in bots:
            # Get the room aliases for this entity from config and resolve to IDs
            room_aliases = get_rooms_for_entity(bot.agent_name, config)
            bot.rooms = resolve_room_aliases(room_aliases)

        # After rooms exist, ensure room invitations are up to date
        await self._ensure_room_invitations()

        # Ensure user joins all rooms after being invited
        # Get all room IDs (not just newly created ones)
        all_rooms = load_rooms()
        all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
        if all_room_ids:
            await ensure_user_in_rooms(MATRIX_HOMESERVER, all_room_ids)

        # Now have bots join their configured rooms
        join_tasks = [bot.ensure_rooms() for bot in bots]
        await asyncio.gather(*join_tasks)
        logger.info("All agents have joined their configured rooms")

    async def _ensure_rooms_exist(self) -> None:
        """Ensure all configured rooms exist, creating them if necessary.

        This uses the router bot's client to create rooms since it has the necessary permissions.
        """
        if ROUTER_AGENT_NAME not in self.agent_bots:
            logger.warning("Router not available, cannot ensure rooms exist")
            return

        router_bot = self.agent_bots[ROUTER_AGENT_NAME]
        if router_bot.client is None:
            logger.warning("Router client not available, cannot ensure rooms exist")
            return

        # Directly create rooms using the router's client
        config = self.config
        if config is None:
            msg = "Configuration not loaded"
            raise RuntimeError(msg)
        room_ids = await ensure_all_rooms_exist(router_bot.client, config)
        logger.info(f"Ensured existence of {len(room_ids)} rooms")

    async def _ensure_room_invitations(self) -> None:  # noqa: C901, PLR0912
        """Ensure all agents and the user are invited to their configured rooms.

        This uses the router bot's client to manage room invitations,
        as the router has admin privileges in all rooms.
        """
        if ROUTER_AGENT_NAME not in self.agent_bots:
            logger.warning("Router not available, cannot ensure room invitations")
            return

        router_bot = self.agent_bots[ROUTER_AGENT_NAME]
        if router_bot.client is None:
            logger.warning("Router client not available, cannot ensure room invitations")
            return

        # Get the current configuration
        config = self.config
        if not config:
            logger.warning("No configuration available, cannot ensure room invitations")
            return

        # Get all rooms the router is in
        joined_rooms = await get_joined_rooms(router_bot.client)
        if not joined_rooms:
            return

        server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)
        authorized_user_ids = _get_authorized_user_ids_to_invite(config)

        # First, invite the user account to all rooms
        state = MatrixState.load()
        user_account = state.get_account(INTERNAL_USER_ACCOUNT_KEY)
        if user_account:
            user_id = MatrixID.from_username(user_account.username, server_name).full_id
            authorized_user_ids.discard(user_id)
            for room_id in joined_rooms:
                room_members = await get_room_members(router_bot.client, room_id)
                if user_id not in room_members:
                    success = await invite_to_room(router_bot.client, room_id, user_id)
                    if success:
                        logger.info(f"Invited user {user_id} to room {room_id}")
                    else:
                        logger.warning(f"Failed to invite user {user_id} to room {room_id}")

        for room_id in joined_rooms:
            # Get who should be in this room based on configuration
            configured_bots = config.get_configured_bots_for_room(room_id)

            if not configured_bots:
                continue

            # Get current members of the room
            current_members = await get_room_members(router_bot.client, room_id)

            # Invite authorized human users for this room
            for authorized_user_id in authorized_user_ids:
                if authorized_user_id in current_members:
                    continue
                if not is_authorized_sender(authorized_user_id, config, room_id):
                    continue

                success = await invite_to_room(router_bot.client, room_id, authorized_user_id)
                if success:
                    logger.info(f"Invited authorized user {authorized_user_id} to room {room_id}")
                else:
                    logger.warning(f"Failed to invite authorized user {authorized_user_id} to room {room_id}")

            # Invite missing bots
            for bot_username in configured_bots:
                bot_user_id = MatrixID.from_username(bot_username, server_name).full_id

                if bot_user_id not in current_members:
                    # Bot should be in room but isn't - invite them
                    success = await invite_to_room(router_bot.client, room_id, bot_user_id)
                    if success:
                        logger.info(f"Invited {bot_username} to room {room_id}")
                    else:
                        logger.warning(f"Failed to invite {bot_username} to room {room_id}")

        logger.info("Ensured room invitations for all configured agents and authorized users")


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
        entities_to_restart.add(ROUTER_AGENT_NAME)

    return entities_to_restart


def _is_concrete_matrix_user_id(user_id: str) -> bool:
    """Return whether this string is a concrete Matrix user ID."""
    return (
        user_id.startswith("@") and ":" in user_id and "*" not in user_id and "?" not in user_id and " " not in user_id
    )


def _get_authorized_user_ids_to_invite(config: Config) -> set[str]:
    """Collect Matrix users from authorization config that can be invited."""
    user_ids = set(config.authorization.global_users)
    for room_users in config.authorization.room_permissions.values():
        user_ids.update(room_users)

    concrete_user_ids = {user_id for user_id in user_ids if _is_concrete_matrix_user_id(user_id)}
    skipped = sorted(user_ids - concrete_user_ids)
    if skipped:
        logger.warning(
            "Skipping non-concrete authorization user IDs for invites",
            user_ids=skipped,
        )
    return concrete_user_ids


def _get_changed_agents(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
    if not config:
        return set()

    changed = set()
    all_agents = set(config.agents.keys()) | set(new_config.agents.keys())

    for agent_name in all_agents:
        old_agent = config.agents.get(agent_name)
        new_agent = new_config.agents.get(agent_name)

        # Compare agents using model_dump with exclude_none=True to match how configs are saved
        # This prevents false positives when None values are involved
        if old_agent and new_agent:
            # Both exist - compare their non-None values (matching save_to_yaml behavior)
            old_dict = old_agent.model_dump(exclude_none=True)
            new_dict = new_agent.model_dump(exclude_none=True)
            agents_differ = old_dict != new_dict
        else:
            # One is None - they definitely differ
            agents_differ = old_agent != new_agent

        old_culture = _culture_signature_for_agent(agent_name, config) if old_agent else None
        new_culture = _culture_signature_for_agent(agent_name, new_config) if new_agent else None
        culture_differ = old_culture != new_culture

        # Only restart if this specific agent's configuration has changed
        # (not just global config changes like authorization). Culture assignment changes
        # are stored outside agent config, so check them explicitly.
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
    assignment = config.get_agent_culture(agent_name)
    if assignment is None:
        return None
    culture_name, culture_config = assignment
    return (culture_name, culture_config.mode, culture_config.description)


def _get_changed_teams(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
    if not config:
        return set()

    changed = set()
    all_teams = set(config.teams.keys()) | set(new_config.teams.keys())

    for team_name in all_teams:
        old_team = config.teams.get(team_name)
        new_team = new_config.teams.get(team_name)

        # Compare teams using model_dump with exclude_none=True to match how configs are saved
        if old_team and new_team:
            old_dict = old_team.model_dump(exclude_none=True)
            new_dict = new_team.model_dump(exclude_none=True)
            teams_differ = old_dict != new_dict
        else:
            teams_differ = old_team != new_team

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


def _create_temp_user(entity_name: str, config: Config) -> AgentMatrixUser:
    """Create a temporary user object that will be updated by ensure_user_account."""
    if entity_name == ROUTER_AGENT_NAME:
        display_name = "RouterAgent"
    elif entity_name in config.agents:
        display_name = config.agents[entity_name].display_name
    elif entity_name in config.teams:
        display_name = config.teams[entity_name].display_name
    else:
        display_name = entity_name

    return AgentMatrixUser(
        agent_name=entity_name,
        user_id="",  # Will be set by ensure_user_account
        display_name=display_name,
        password="",  # Will be set by ensure_user_account
    )


async def _cancel_sync_task(entity_name: str, sync_tasks: dict[str, asyncio.Task]) -> None:
    """Cancel and remove a sync task for an entity."""
    if entity_name in sync_tasks:
        task = sync_tasks[entity_name]
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        del sync_tasks[entity_name]


async def _stop_entities(
    entities_to_restart: set[str],
    agent_bots: dict[str, Any],
    sync_tasks: dict[str, asyncio.Task],
) -> None:
    # Cancel sync tasks to prevent duplicates
    for entity_name in entities_to_restart:
        await _cancel_sync_task(entity_name, sync_tasks)

    stop_tasks = []
    for entity_name in entities_to_restart:
        if entity_name in agent_bots:
            bot = agent_bots[entity_name]
            stop_tasks.append(bot.stop())

    if stop_tasks:
        await asyncio.gather(*stop_tasks)

    for entity_name in entities_to_restart:
        agent_bots.pop(entity_name, None)


async def _sync_forever_with_restart(bot: AgentBot | TeamBot, max_retries: int = -1) -> None:
    """Run sync_forever with automatic restart on failure.

    Args:
        bot: The bot to run sync for
        max_retries: Maximum number of retries (-1 for infinite)

    """
    retry_count = 0
    while bot.running and (max_retries < 0 or retry_count < max_retries):
        try:
            logger.info(f"Starting sync loop for {bot.agent_name}")
            await bot.sync_forever()
            # If sync_forever returns normally, the bot was stopped intentionally
            break
        except asyncio.CancelledError:
            # Task was cancelled, exit gracefully
            logger.info(f"Sync task for {bot.agent_name} was cancelled")
            break
        except Exception:
            retry_count += 1
            logger.exception(f"Sync loop failed for {bot.agent_name} (retry {retry_count})")

            if not bot.running:
                # Bot was stopped, don't restart
                break

            if max_retries >= 0 and retry_count >= max_retries:
                logger.exception(f"Max retries ({max_retries}) reached for {bot.agent_name}, giving up")
                break

            # Wait a bit before restarting to avoid rapid restarts
            wait_time = min(60, 5 * retry_count)  # Exponential backoff, max 60 seconds
            logger.info(f"Restarting sync loop for {bot.agent_name} in {wait_time} seconds...")
            await asyncio.sleep(wait_time)


async def _handle_config_change(orchestrator: MultiAgentOrchestrator, stop_watching: asyncio.Event) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed, checking for updates...")
    if orchestrator.running:
        updated = await orchestrator.update_config()
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")
    if not orchestrator.running:
        stop_watching.set()


async def _watch_config_task(config_path: Path, orchestrator: MultiAgentOrchestrator) -> None:
    """Watch config file for changes."""
    stop_watching = asyncio.Event()

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator, stop_watching)

    await watch_file(config_path, on_config_change, stop_watching)


async def _watch_skills_task(orchestrator: MultiAgentOrchestrator) -> None:
    """Watch skill roots for changes and clear cached skills."""
    # Wait for orchestrator to start before watching
    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    last_snapshot = get_skill_snapshot()
    while orchestrator.running:
        await asyncio.sleep(1.0)
        snapshot = get_skill_snapshot()
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            clear_skill_cache()
            logger.info("Skills changed; cache cleared")


async def _run_api_server(host: str, port: int, log_level: str) -> None:
    """Run the dashboard API server as an asyncio task."""
    from mindroom.api.main import app as api_app  # noqa: PLC0415  # avoid heavy import at module level

    config = uvicorn.Config(api_app, host=host, port=port, log_level=log_level.lower())
    server = uvicorn.Server(config)
    await server.serve()


async def main(
    log_level: str,
    storage_path: Path,
    *,
    api: bool = True,
    api_port: int = 8765,
    api_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
        storage_path: The base directory for storing agent data
        api: Whether to start the dashboard API server
        api_port: Port for the dashboard API server
        api_host: Host for the dashboard API server

    """
    # Set up logging with the specified level
    setup_logging(level=log_level)

    # Canonicalize once at startup so all downstream storage paths are cwd-stable.
    storage_path = storage_path.expanduser().resolve()

    # Sync API keys from environment to CredentialsManager
    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()

    # Create storage directory if it doesn't exist
    storage_path.mkdir(parents=True, exist_ok=True)

    # Get config file path
    config_path = Path(CONFIG_PATH)

    # Create and start orchestrator
    logger.info("Starting orchestrator...")
    orchestrator = MultiAgentOrchestrator(storage_path=storage_path)

    try:
        # Create task to run the orchestrator
        orchestrator_task = asyncio.create_task(orchestrator.start())

        # Create task to watch config file for changes
        watcher_task = asyncio.create_task(_watch_config_task(config_path, orchestrator))

        # Create task to watch skills for changes
        skills_watcher_task = asyncio.create_task(_watch_skills_task(orchestrator))

        tasks = {orchestrator_task, watcher_task, skills_watcher_task}

        # Optionally start the dashboard API server
        if api:
            logger.info("Starting dashboard API server on %s:%d", api_host, api_port)
            api_task = asyncio.create_task(_run_api_server(api_host, api_port, log_level))
            tasks.add(api_task)

        # Wait for any task to complete (or fail)
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check if any completed task had an exception
        for task in done:
            try:
                task.result()  # This will raise if the task had an exception
            except asyncio.CancelledError:
                logger.info("Task was cancelled")
            except Exception:
                logger.exception("Task failed with exception")
                # Don't re-raise - let cleanup happen gracefully

        # Cancel any pending tasks
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    except Exception:
        logger.exception("Error in orchestrator")
    finally:
        # Final cleanup
        if orchestrator is not None:
            await orchestrator.stop()
