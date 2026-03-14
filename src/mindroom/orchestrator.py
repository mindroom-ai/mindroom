"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import uvicorn

from mindroom.agents import ensure_default_agent_workspaces, get_rooms_for_entity
from mindroom.authorization import is_authorized_sender
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.knowledge.manager import initialize_knowledge_managers, shutdown_knowledge_managers
from mindroom.matrix.client import (
    PermanentMatrixStartupError,
    get_joined_rooms,
    get_room_members,
    invite_to_room,
)
from mindroom.matrix.identity import MatrixID, extract_server_name_from_homeserver
from mindroom.matrix.rooms import (
    ensure_all_rooms_exist,
    ensure_root_space,
    ensure_user_in_rooms,
    load_rooms,
    resolve_room_aliases,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, INTERNAL_USER_AGENT_NAME, create_agent_user
from mindroom.memory.auto_flush import MemoryAutoFlushWorker, auto_flush_enabled
from mindroom.runtime_state import (
    reset_runtime_state,
    set_runtime_failed,
    set_runtime_ready,
    set_runtime_starting,
)
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot

from .bot import AgentBot, TeamBot, create_bot_for_entity
from .config.main import Config
from .constants import CONFIG_PATH, MATRIX_HOMESERVER, set_runtime_storage_path
from .credentials_sync import sync_env_to_credentials
from .file_watcher import watch_file
from .logging_config import get_logger, setup_logging
from .orchestration.config_updates import (
    ConfigUpdatePlan,
    build_config_update_plan,
)
from .orchestration.rooms import (
    get_authorized_user_ids_to_invite,
    get_root_space_user_ids_to_invite,
)
from .orchestration.runtime import (
    STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    STARTUP_RETRY_MAX_DELAY_SECONDS,
    EntityStartResults,
    cancel_sync_task,
    cancel_task,
    create_logged_task,
    create_temp_user,
    is_permanent_startup_error,
    retry_delay_seconds,
    run_with_retry,
    stop_entities,
    sync_forever_with_restart,
    wait_for_matrix_homeserver,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .knowledge.manager import KnowledgeManager

logger = get_logger(__name__)

_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS = 1.0
_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS = 30.0


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    config_path: Path = field(default_factory=lambda: Path(CONFIG_PATH))
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _sync_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    _bot_start_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    knowledge_managers: dict[str, KnowledgeManager] = field(default_factory=dict, init=False)
    _memory_auto_flush_worker: MemoryAutoFlushWorker | None = field(default=None, init=False)
    _memory_auto_flush_task: asyncio.Task | None = field(default=None, init=False)
    _knowledge_refresh_task: asyncio.Task | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Store a canonical absolute storage path to survive runtime cwd changes."""
        self.storage_path = self.storage_path.expanduser().resolve()
        self.config_path = self.config_path.expanduser().resolve()

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

        if not auto_flush_enabled(config):
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

        This reuses the same `create_agent_user` flow that agents use,
        treating the user as a special internal "agent" account.
        Skipped when `mindroom_user` is not configured, such as hosted/public profiles.
        """
        if config.mindroom_user is None:
            logger.debug("mindroom_user not configured, skipping user account creation")
            return
        # The user account is managed through the same Matrix account lifecycle as bots.
        user_account = await create_agent_user(
            MATRIX_HOMESERVER,
            INTERNAL_USER_AGENT_NAME,
            config.mindroom_user.display_name,
            username=config.mindroom_user.username,
        )
        logger.info(f"User account ready: {user_account.user_id}")

    def _require_config(self) -> Config:
        """Return the active config or fail fast if it has not been loaded."""
        config = self.config
        if config is None:
            msg = "Configuration not loaded"
            raise RuntimeError(msg)
        return config

    async def _prepare_user_account(
        self,
        config: Config,
        *,
        update_runtime_state: bool,
    ) -> None:
        """Ensure the internal user account exists, retrying only transient failures."""
        await run_with_retry(
            "Preparing MindRoom user account",
            lambda: self._ensure_user_account(config),
            permanent_error_check=is_permanent_startup_error,
            update_runtime_state=update_runtime_state,
        )

    async def _configure_knowledge(self, config: Config, *, start_watcher: bool) -> None:
        """Initialize or reconfigure knowledge managers for the current config."""
        self.knowledge_managers = await initialize_knowledge_managers(
            config=config,
            storage_path=self.storage_path,
            start_watchers=start_watcher,
            reindex_on_create=False,
        )

    async def _cancel_knowledge_refresh_task(self) -> None:
        """Cancel any in-flight background knowledge refresh task."""
        task = self._knowledge_refresh_task
        self._knowledge_refresh_task = None
        await cancel_task(task, suppress_exceptions=(asyncio.CancelledError, Exception))

    async def _cancel_bot_start_task(self, entity_name: str) -> None:
        """Cancel any background start task for one bot."""
        task = self._bot_start_tasks.pop(entity_name, None)
        await cancel_task(task)

    async def _cancel_bot_start_tasks(self) -> None:
        """Cancel all background bot start tasks."""
        for entity_name in tuple(self._bot_start_tasks):
            await self._cancel_bot_start_task(entity_name)

    def _start_sync_task(self, entity_name: str, bot: AgentBot | TeamBot) -> None:
        """Ensure one sync task exists for a running bot."""
        existing_task = self._sync_tasks.get(entity_name)
        if existing_task is not None and not existing_task.done():
            return
        self._sync_tasks[entity_name] = asyncio.create_task(
            sync_forever_with_restart(bot),
            name=f"sync_{entity_name}",
        )

    def _bots_to_setup_after_background_start(self, entity_name: str) -> list[AgentBot | TeamBot]:
        """Return the bots whose room memberships should be reconciled after a background start."""
        if entity_name == ROUTER_AGENT_NAME:
            return self._running_bots_for_entities(self.agent_bots)
        return self._running_bots_for_entities((entity_name,))

    def _running_bots_for_entities(self, entity_names: Iterable[str]) -> list[AgentBot | TeamBot]:
        """Return running bots for the given entity names."""
        running_bots: list[AgentBot | TeamBot] = []
        for entity_name in entity_names:
            bot = self.agent_bots.get(entity_name)
            if bot is not None and bot.running:
                running_bots.append(bot)
        return running_bots

    async def _try_start_bot_once(self, entity_name: str, bot: AgentBot | TeamBot) -> bool | None:
        """Run one bot start attempt and classify the result."""
        try:
            return bool(await bot.try_start())
        except PermanentMatrixStartupError:
            logger.error(  # noqa: TRY400
                "Bot startup failed permanently; leaving bot disabled until configuration changes",
                agent_name=entity_name,
            )
            return None

    async def _run_bot_start_retry(self, entity_name: str) -> None:
        """Keep retrying one bot start until it succeeds or the task is cancelled."""
        current_task = asyncio.current_task()
        attempt = 0
        try:
            while True:
                bot = self.agent_bots.get(entity_name)
                if bot is None:
                    return

                start_status = await self._try_start_bot_once(entity_name, bot)
                if start_status is None:
                    return
                if start_status:
                    logger.info("Bot recovered after startup failure", agent_name=entity_name)
                    bots_to_setup = self._bots_to_setup_after_background_start(entity_name)
                    if bots_to_setup:
                        await run_with_retry(
                            f"Updating Matrix room memberships for {entity_name}",
                            partial(self._setup_rooms_and_memberships, bots_to_setup),
                            update_runtime_state=False,
                        )
                    self._start_sync_task(entity_name, bot)
                    return

                attempt += 1
                retry_in_seconds = retry_delay_seconds(
                    attempt,
                    initial_delay_seconds=STARTUP_RETRY_INITIAL_DELAY_SECONDS,
                    max_delay_seconds=STARTUP_RETRY_MAX_DELAY_SECONDS,
                )
                logger.warning(
                    "Bot startup failed; retrying in background",
                    agent_name=entity_name,
                    attempt=attempt,
                    retry_in_seconds=retry_in_seconds,
                )
                await asyncio.sleep(retry_in_seconds)
        finally:
            if self._bot_start_tasks.get(entity_name) is current_task:
                del self._bot_start_tasks[entity_name]

    async def _schedule_bot_start_retry(self, entity_name: str) -> None:
        """Schedule background retries for one failed bot startup."""
        await self._cancel_bot_start_task(entity_name)
        self._bot_start_tasks[entity_name] = create_logged_task(
            self._run_bot_start_retry(entity_name),
            name=f"retry_start_{entity_name}",
            failure_message="Background bot start task failed",
        )

    async def _run_knowledge_refresh(
        self,
        config: Config,
        *,
        start_watcher: bool,
    ) -> None:
        """Run background knowledge refresh until it succeeds or is cancelled."""
        current_task = asyncio.current_task()
        try:
            await run_with_retry(
                "Background knowledge refresh",
                lambda: self._configure_knowledge(config, start_watcher=start_watcher),
                update_runtime_state=False,
            )
        finally:
            if self._knowledge_refresh_task is current_task:
                self._knowledge_refresh_task = None

    async def _schedule_knowledge_refresh(
        self,
        config: Config,
        *,
        start_watcher: bool,
    ) -> None:
        """Schedule knowledge refresh in the background, replacing any in-flight run."""
        await self._cancel_knowledge_refresh_task()
        self._knowledge_refresh_task = create_logged_task(
            self._run_knowledge_refresh(config, start_watcher=start_watcher),
            name="knowledge_refresh",
            failure_message="Background knowledge refresh failed",
        )

    async def _refresh_knowledge_for_runtime(
        self,
        config: Config,
        *,
        start_watcher: bool,
    ) -> None:
        """Refresh knowledge now (startup path) or in background (runtime updates)."""
        if self.running:
            await self._schedule_knowledge_refresh(config, start_watcher=start_watcher)
            return
        await self._configure_knowledge(config, start_watcher=start_watcher)

    async def _sync_runtime_support_services(
        self,
        config: Config,
        *,
        start_watcher: bool,
    ) -> None:
        """Refresh runtime support services that depend on the active config."""
        ensure_default_agent_workspaces(config, self.storage_path)
        await self._refresh_knowledge_for_runtime(config, start_watcher=start_watcher)
        await self._sync_memory_auto_flush_worker()

    @staticmethod
    def _configured_entity_names(config: Config) -> list[str]:
        """Return configured entity names with the router first."""
        return [ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys()]

    def _create_managed_bot(self, entity_name: str, config: Config) -> AgentBot | TeamBot:
        """Create and register one runtime-managed bot."""
        temp_user = create_temp_user(entity_name, config)
        bot = cast(
            "AgentBot | TeamBot",
            create_bot_for_entity(
                entity_name,
                temp_user,
                config,
                self.storage_path,
                config_path=self.config_path,
            ),
        )
        bot.orchestrator = self
        self.agent_bots[entity_name] = bot
        return bot

    async def _start_entities_once(
        self,
        entity_names: Iterable[str],
        *,
        start_sync_tasks: bool,
    ) -> EntityStartResults:
        """Try to start each named entity once and classify the results."""
        entity_bots: list[tuple[str, AgentBot | TeamBot]] = []
        for entity_name in entity_names:
            bot = self.agent_bots.get(entity_name)
            if bot is not None:
                entity_bots.append((entity_name, bot))

        results = EntityStartResults()
        if not entity_bots:
            return results

        start_statuses = await asyncio.gather(
            *[self._try_start_bot_once(entity_name, bot) for entity_name, bot in entity_bots],
        )
        for (entity_name, bot), start_status in zip(entity_bots, start_statuses):
            if start_status:
                results.started_bots.append(bot)
                if start_sync_tasks:
                    self._start_sync_task(entity_name, bot)
                continue
            if start_status is None:
                results.permanently_failed_entities.append(entity_name)
                continue
            results.retryable_entities.append(entity_name)
        return results

    async def _create_and_start_entities(
        self,
        entity_names: set[str],
        config: Config,
        *,
        start_sync_tasks: bool,
    ) -> EntityStartResults:
        """Create configured entities and try to start them once."""
        for entity_name in entity_names:
            self._create_managed_bot(entity_name, config)
        return await self._start_entities_once(entity_names, start_sync_tasks=start_sync_tasks)

    async def initialize(self) -> None:
        """Initialize all managed bots from configuration."""
        set_runtime_starting("Loading config and preparing agents")
        logger.info("Initializing multi-agent system...")

        config = Config.from_yaml(self.config_path)
        load_plugins(config)
        await self._prepare_user_account(config, update_runtime_state=True)
        self.config = config
        for entity_name in self._configured_entity_names(config):
            self._create_managed_bot(entity_name, config)

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots and publish readiness state."""
        try:
            await self._start_runtime()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            set_runtime_failed(str(exc))
            raise

    async def _start_router_bot(self) -> AgentBot | TeamBot:
        """Start the router bot, retrying until it succeeds."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None:
            msg = "Router bot is required for startup"
            raise RuntimeError(msg)

        async def _start_router() -> None:
            if await router_bot.try_start():
                return
            msg = "Router bot failed to start"
            raise RuntimeError(msg)

        set_runtime_starting("Starting router Matrix account")
        await run_with_retry(
            "Starting router Matrix account",
            _start_router,
            permanent_error_check=is_permanent_startup_error,
        )
        return router_bot

    def _log_degraded_startup(self, failed_agents: list[str]) -> None:
        """Log degraded startup status for failed non-router bots."""
        if failed_agents:
            logger.warning(
                f"System starting in degraded mode. "
                f"Failed agents: {', '.join(failed_agents)} "
                f"({len(self.agent_bots) - len(failed_agents)}/{len(self.agent_bots)} operational)",
            )
            return
        logger.info("All agent bots started successfully")

    async def _start_runtime(self) -> None:
        """Run the startup sequence before handing off to the sync loops."""
        await wait_for_matrix_homeserver()
        if not self.agent_bots:
            await self.initialize()

        router_bot = await self._start_router_bot()
        set_runtime_starting("Starting remaining Matrix bot accounts")
        start_results = await self._start_entities_once(
            [entity_name for entity_name in self.agent_bots if entity_name != ROUTER_AGENT_NAME],
            start_sync_tasks=False,
        )
        started_bots = [router_bot, *start_results.started_bots]
        self._log_degraded_startup(
            [*start_results.retryable_entities, *start_results.permanently_failed_entities],
        )

        config = self._require_config()

        # Setup rooms and have all bots join them before potentially heavy
        # knowledge indexing, so new rooms and invites are not delayed by embeddings.
        await run_with_retry(
            "Setting up Matrix rooms and memberships",
            lambda: self._setup_rooms_and_memberships(started_bots),
        )

        self.running = True

        # Knowledge refresh is optional for initial availability.
        set_runtime_starting("Refreshing knowledge bases in background")
        await self._schedule_knowledge_refresh(config, start_watcher=True)

        set_runtime_starting("Starting background workers")
        await self._sync_memory_auto_flush_worker()

        # Create sync tasks for each bot with automatic restart on failure.
        set_runtime_starting("Starting Matrix sync loops")
        for entity_name, bot in self.agent_bots.items():
            if bot.running:
                self._start_sync_task(entity_name, bot)

        for entity_name in start_results.retryable_entities:
            await self._schedule_bot_start_retry(entity_name)

        set_runtime_ready()
        # Run all sync tasks until shutdown.
        await asyncio.gather(*tuple(self._sync_tasks.values()))

    async def _load_initial_config(self, new_config: Config) -> bool:
        """Handle config loading before the runtime has an active config."""
        await self._prepare_user_account(new_config, update_runtime_state=not self.running)
        self.config = new_config
        await self._sync_runtime_support_services(new_config, start_watcher=self.running)
        return False

    async def _update_unchanged_bots(self, plan: ConfigUpdatePlan) -> None:
        """Apply the new config to bots that do not require restart."""
        for entity_name, bot in self.agent_bots.items():
            if entity_name in plan.entities_to_restart:
                continue
            bot.config = plan.new_config
            bot.enable_streaming = plan.new_config.defaults.enable_streaming
            await bot._set_presence_with_model_info()
            logger.debug(f"Updated config for {entity_name}")

    async def _remove_deleted_entities(self, removed_entities: set[str]) -> None:
        """Cancel, clean up, and unregister entities removed from config."""
        for entity_name in removed_entities:
            await self._cancel_bot_start_task(entity_name)
            await cancel_sync_task(entity_name, self._sync_tasks)

            bot = self.agent_bots.pop(entity_name, None)
            if bot is not None:
                await bot.cleanup()

    async def _restart_changed_entities(self, plan: ConfigUpdatePlan) -> tuple[set[str], list[str], list[str]]:
        """Restart or create entities affected by the config change."""
        if plan.entities_to_restart:
            for entity_name in plan.entities_to_restart:
                await self._cancel_bot_start_task(entity_name)
            await stop_entities(plan.entities_to_restart, self.agent_bots, self._sync_tasks)

        entities_to_recreate = plan.entities_to_restart & plan.all_new_entities
        changed_entities = entities_to_recreate | plan.new_entities
        start_results = await self._create_and_start_entities(
            changed_entities,
            plan.new_config,
            start_sync_tasks=True,
        )

        removed_restarted_entities = plan.entities_to_restart - plan.all_new_entities
        for entity_name in removed_restarted_entities:
            self.agent_bots.pop(entity_name, None)

        await self._remove_deleted_entities(plan.removed_entities)
        return changed_entities, start_results.retryable_entities, start_results.permanently_failed_entities

    async def _reconcile_post_update_rooms(
        self,
        plan: ConfigUpdatePlan,
        changed_entities: set[str],
    ) -> None:
        """Reconcile rooms and memberships after entity/config updates."""
        bots_to_setup = self._running_bots_for_entities(changed_entities)
        if bots_to_setup or plan.mindroom_user_changed or plan.matrix_room_access_changed or plan.authorization_changed:
            await self._setup_rooms_and_memberships(bots_to_setup)
            return
        if plan.matrix_space_changed:
            room_ids = await self._ensure_rooms_exist()
            await self._ensure_root_space(room_ids)

    async def update_config(self) -> bool:
        """Reload configuration, restart affected entities, and reconcile room state."""
        new_config = Config.from_yaml(self.config_path)
        load_plugins(new_config)

        if not self.config:
            return await self._load_initial_config(new_config)

        current_config = self._require_config()
        plan = build_config_update_plan(
            current_config=current_config,
            new_config=new_config,
            configured_entities=set(self._configured_entity_names(new_config)),
            existing_entities=set(self.agent_bots.keys()),
            agent_bots=self.agent_bots,
        )

        if plan.mindroom_user_changed:
            await self._prepare_user_account(new_config, update_runtime_state=not self.running)

        # Only apply the new config after validation and account checks succeed.
        self.config = new_config
        logger.info(f"Updating config. New authorization: {new_config.authorization.global_users}")
        await self._update_unchanged_bots(plan)

        if plan.only_support_service_changes:
            await self._sync_runtime_support_services(new_config, start_watcher=self.running)
            return False

        changed_entities, retryable_entities, permanently_failed_entities = await self._restart_changed_entities(plan)
        await self._reconcile_post_update_rooms(plan, changed_entities)

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

    def _router_bot(self) -> AgentBot | TeamBot | None:
        """Return the router bot when it exists and has an active client."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None:
            logger.warning("Router not available")
            return None
        if router_bot.client is None:
            logger.warning("Router client not available")
            return None
        return router_bot

    async def _setup_rooms_and_memberships(self, bots: list[AgentBot | TeamBot]) -> None:
        """Setup rooms and ensure all bots have correct memberships.

        This shared flow is used during both initial startup and config updates.
        """
        # Ensure all configured rooms exist before reconciling memberships.
        room_ids = await self._ensure_rooms_exist()
        await self._ensure_root_space(room_ids)

        # Resolve room aliases now that any missing rooms have been created.
        config = self._require_config()
        for bot in bots:
            room_aliases = get_rooms_for_entity(bot.agent_name, config)
            bot.rooms = resolve_room_aliases(room_aliases)

        async def _ensure_internal_user_memberships() -> None:
            all_rooms = load_rooms()
            all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
            if all_room_ids and config.mindroom_user is not None:
                await ensure_user_in_rooms(MATRIX_HOMESERVER, all_room_ids)

        # First invitation and join pass for rooms the router already manages.
        await self._ensure_room_invitations()
        await _ensure_internal_user_memberships()
        await asyncio.gather(*(bot.ensure_rooms() for bot in bots))

        # Existing invite-only rooms may only become manageable after the router joins.
        # Rerun room reconciliation so topic and access policy updates apply in that case.
        if any(bot.agent_name == ROUTER_AGENT_NAME for bot in bots):
            room_ids = await self._ensure_rooms_exist()
            await self._ensure_root_space(room_ids)

        # Retry invitations once the router has completed its first join pass.
        await self._ensure_room_invitations()
        await _ensure_internal_user_memberships()

        follow_up_bots = [bot for bot in bots if bot.agent_name != ROUTER_AGENT_NAME]
        if follow_up_bots:
            await asyncio.gather(*(bot.ensure_rooms() for bot in follow_up_bots))

        logger.info("All agents have joined their configured rooms")

    async def _ensure_rooms_exist(self) -> dict[str, str]:
        """Ensure all configured rooms exist, creating them if necessary.

        The router bot performs room creation because it holds the required permissions.
        """
        router_bot = self._router_bot()
        if router_bot is None:
            return {}
        assert router_bot.client is not None

        config = self._require_config()
        room_ids = await ensure_all_rooms_exist(router_bot.client, config)
        logger.info(f"Ensured existence of {len(room_ids)} rooms")
        return room_ids

    async def _ensure_root_space(self, room_ids: dict[str, str] | None = None) -> None:
        """Ensure the optional root Matrix Space exists and link the current managed rooms."""
        router_bot = self._router_bot()
        if router_bot is None:
            return
        assert router_bot.client is not None

        config = self._require_config()
        if not config.matrix_space.enabled:
            return

        normalized_room_ids = room_ids if isinstance(room_ids, dict) else {}
        root_space_id = await ensure_root_space(router_bot.client, config, normalized_room_ids)
        if root_space_id is None:
            return

        invite_user_ids = get_root_space_user_ids_to_invite(config)
        if not invite_user_ids:
            return

        current_members = await get_room_members(router_bot.client, root_space_id)
        for user_id in sorted(invite_user_ids):
            if user_id in current_members:
                continue
            success = await invite_to_room(router_bot.client, root_space_id, user_id)
            if success:
                logger.info(f"Invited user {user_id} to root space {root_space_id}")
            else:
                logger.warning(f"Failed to invite user {user_id} to root space {root_space_id}")

    async def _invite_user_if_missing(
        self,
        room_id: str,
        user_id: str,
        current_members: set[str],
        *,
        success_message: str,
        failure_message: str,
    ) -> None:
        """Invite one user if they are not already a member."""
        router_bot = self._router_bot()
        if router_bot is None:
            return
        assert router_bot.client is not None
        if user_id in current_members:
            return
        success = await invite_to_room(router_bot.client, room_id, user_id)
        if success:
            logger.info(success_message)
            current_members.add(user_id)
        else:
            logger.warning(failure_message)

    async def _invite_internal_user_to_rooms(
        self,
        config: Config,
        joined_rooms: list[str],
        authorized_user_ids: set[str],
    ) -> set[str]:
        """Invite the configured internal user to all joined rooms when needed."""
        router_bot = self._router_bot()
        if router_bot is None:
            return authorized_user_ids
        assert router_bot.client is not None

        state = MatrixState.load()
        user_account = state.get_account(INTERNAL_USER_ACCOUNT_KEY)
        if config.mindroom_user is None or not user_account:
            return authorized_user_ids

        server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)
        user_id = MatrixID.from_username(user_account.username, server_name).full_id
        authorized_user_ids.discard(user_id)
        for room_id in joined_rooms:
            room_members = await get_room_members(router_bot.client, room_id)
            await self._invite_user_if_missing(
                room_id,
                user_id,
                room_members,
                success_message=f"Invited user {user_id} to room {room_id}",
                failure_message=f"Failed to invite user {user_id} to room {room_id}",
            )
        return authorized_user_ids

    async def _invite_authorized_users_to_room(
        self,
        room_id: str,
        current_members: set[str],
        authorized_user_ids: set[str],
        config: Config,
    ) -> None:
        """Invite authorized human users who can access a given room."""
        for authorized_user_id in authorized_user_ids:
            if not is_authorized_sender(authorized_user_id, config, room_id):
                continue
            await self._invite_user_if_missing(
                room_id,
                authorized_user_id,
                current_members,
                success_message=f"Invited authorized user {authorized_user_id} to room {room_id}",
                failure_message=f"Failed to invite authorized user {authorized_user_id} to room {room_id}",
            )

    async def _invite_configured_bots_to_room(
        self,
        room_id: str,
        current_members: set[str],
        configured_bots: Iterable[str],
        server_name: str,
    ) -> None:
        """Invite all configured bots for a room."""
        for bot_username in configured_bots:
            bot_user_id = MatrixID.from_username(bot_username, server_name).full_id
            await self._invite_user_if_missing(
                room_id,
                bot_user_id,
                current_members,
                success_message=f"Invited {bot_username} to room {room_id}",
                failure_message=f"Failed to invite {bot_username} to room {room_id}",
            )

    async def _ensure_room_invitations(self) -> None:
        """Ensure all agents and the internal user are invited to their configured rooms.

        The router client performs these invitations because it has admin privileges
        across the managed rooms.
        """
        router_bot = self._router_bot()
        if router_bot is None:
            return
        assert router_bot.client is not None

        config = self.config
        if not config:
            logger.warning("No configuration available, cannot ensure room invitations")
            return

        joined_rooms = await get_joined_rooms(router_bot.client)
        if not joined_rooms:
            return

        server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)
        authorized_user_ids = get_authorized_user_ids_to_invite(config)
        authorized_user_ids = await self._invite_internal_user_to_rooms(
            config,
            joined_rooms,
            authorized_user_ids,
        )

        for room_id in joined_rooms:
            configured_bots = config.get_configured_bots_for_room(room_id)
            if not configured_bots:
                continue

            current_members = await get_room_members(router_bot.client, room_id)
            await self._invite_authorized_users_to_room(room_id, current_members, authorized_user_ids, config)
            await self._invite_configured_bots_to_room(room_id, current_members, configured_bots, server_name)

        logger.info("Ensured room invitations for all configured agents and authorized users")

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        await self._stop_memory_auto_flush_worker()
        await self._cancel_knowledge_refresh_task()
        await self._cancel_bot_start_tasks()
        await shutdown_knowledge_managers()
        self.knowledge_managers = {}

        # Cancel sync tasks first so shutdown does not race with active sync loops.
        for entity_name in list(self._sync_tasks.keys()):
            await cancel_sync_task(entity_name, self._sync_tasks)

        for bot in self.agent_bots.values():
            bot.running = False

        stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")


async def _handle_config_change(orchestrator: MultiAgentOrchestrator) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed, checking for updates...")
    if orchestrator.running:
        updated = await orchestrator.update_config()
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")
        return
    logger.info("Ignoring config change while startup is still in progress")


async def _watch_config_task(config_path: Path, orchestrator: MultiAgentOrchestrator) -> None:
    """Watch config file for changes."""

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator)

    await watch_file(config_path, on_config_change)


async def _watch_skills_task(orchestrator: MultiAgentOrchestrator) -> None:
    """Watch skill roots for changes and clear cached skills."""
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
    """Run the bundled dashboard/API server as an asyncio task."""
    from mindroom.api.main import app as api_app  # noqa: PLC0415

    config = uvicorn.Config(api_app, host=host, port=port, log_level=log_level.lower())
    server = uvicorn.Server(config)
    await server.serve()


async def _run_auxiliary_task_forever(
    task_name: str,
    operation: Callable[[], Awaitable[None]],
) -> None:
    """Restart a non-critical background task whenever it exits or crashes."""
    restart_count = 0
    while True:
        started_at = time.monotonic()
        try:
            await operation()
            logger.warning("Auxiliary task exited; restarting", task_name=task_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auxiliary task crashed; restarting", task_name=task_name)
        if time.monotonic() - started_at >= _AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS:
            restart_count = 0
        restart_count += 1
        await asyncio.sleep(
            retry_delay_seconds(
                restart_count,
                initial_delay_seconds=_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS,
                max_delay_seconds=_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS,
            ),
        )


async def main(
    log_level: str,
    storage_path: Path,
    *,
    api: bool = True,
    api_port: int = 8765,
    api_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Main entry point for the multi-agent bot system."""
    storage_path = set_runtime_storage_path(storage_path)

    # Configure logging before any background tasks or account setup begin.
    setup_logging(level=log_level)

    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()

    # Ensure storage exists before any runtime components try to write into it.
    storage_path.mkdir(parents=True, exist_ok=True)

    logger.info("Starting orchestrator...")
    orchestrator = MultiAgentOrchestrator(storage_path=storage_path, config_path=Path(CONFIG_PATH))
    set_runtime_starting()
    auxiliary_tasks: list[asyncio.Task] = []

    try:
        auxiliary_specs = [
            (
                "config watcher",
                lambda: _watch_config_task(orchestrator.config_path, orchestrator),
                "config_watcher_supervisor",
            ),
            ("skills watcher", lambda: _watch_skills_task(orchestrator), "skills_watcher_supervisor"),
        ]

        if api:
            # Optionally run the bundled dashboard/API server alongside the orchestrator.
            logger.info("Starting bundled dashboard/API server on %s:%d", api_host, api_port)
            auxiliary_specs.append(
                ("bundled API server", lambda: _run_api_server(api_host, api_port, log_level), "api_server_supervisor"),
            )

        for task_name, operation, supervisor_name in auxiliary_specs:
            auxiliary_tasks.append(
                asyncio.create_task(
                    _run_auxiliary_task_forever(task_name, operation),
                    name=supervisor_name,
                ),
            )

        await orchestrator.start()

    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    except Exception:
        logger.exception("Error in orchestrator")
        raise
    finally:
        # Cancel auxiliary supervisors before shutting down the orchestrator itself.
        for task in auxiliary_tasks:
            task.cancel()
        for task in auxiliary_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await orchestrator.stop()
        reset_runtime_state()
