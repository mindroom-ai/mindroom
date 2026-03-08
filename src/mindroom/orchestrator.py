"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn

from mindroom.memory.auto_flush import MemoryAutoFlushWorker, auto_flush_enabled
from mindroom.runtime_state import reset_runtime_state, set_runtime_failed, set_runtime_ready, set_runtime_starting
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot

from .agents import get_rooms_for_entity
from .authorization import is_authorized_sender
from .bot import AgentBot, TeamBot, create_bot_for_entity
from .config.main import Config
from .constants import CONFIG_PATH, MATRIX_HOMESERVER, MATRIX_SSL_VERIFY, ROUTER_AGENT_NAME
from .credentials_sync import sync_env_to_credentials
from .file_watcher import watch_file
from .knowledge.manager import initialize_knowledge_managers, shutdown_knowledge_managers
from .logging_config import get_logger, setup_logging
from .matrix.client import PermanentMatrixStartupError, get_joined_rooms, get_room_members, invite_to_room
from .matrix.health import matrix_versions_url, response_has_matrix_versions
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
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from pydantic import BaseModel

    from .knowledge.manager import KnowledgeManager

logger = get_logger(__name__)

_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV = "MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS"
_MATRIX_HOMESERVER_REQUEST_TIMEOUT_SECONDS = 5.0
_MATRIX_HOMESERVER_RETRY_INTERVAL_SECONDS = 2.0
_STARTUP_RETRY_INITIAL_DELAY_SECONDS = 2.0
_STARTUP_RETRY_MAX_DELAY_SECONDS = 60.0
_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS = 1.0
_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS = 30.0


def _matrix_homeserver_startup_timeout_seconds_from_env() -> int | None:
    """Return the startup wait timeout from the environment, if configured."""
    raw_timeout = os.getenv(_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV, "").strip()
    if not raw_timeout:
        return None
    timeout_seconds = int(raw_timeout)
    if timeout_seconds == 0:
        return None
    if timeout_seconds < 0:
        msg = f"{_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV} must be 0 or a positive integer"
        raise ValueError(msg)
    return timeout_seconds


def _retry_delay_seconds(
    attempt: int,
    *,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> float:
    """Return capped exponential backoff delay for a retry attempt."""
    exponent = max(0, attempt - 1)
    return min(max_delay_seconds, initial_delay_seconds * (2**exponent))


def _is_permanent_startup_error(exc: Exception) -> bool:
    """Return whether a startup exception is clearly non-retryable."""
    return isinstance(exc, PermanentMatrixStartupError)


def _config_entries_differ(old_entry: BaseModel | None, new_entry: BaseModel | None) -> bool:
    """Compare optional config models using the same shape as persisted YAML."""
    if old_entry is None or new_entry is None:
        return old_entry != new_entry
    return old_entry.model_dump(exclude_none=True) != new_entry.model_dump(exclude_none=True)


async def _cancel_task(
    task: asyncio.Task | None,
    *,
    suppress_exceptions: tuple[type[BaseException], ...] = (asyncio.CancelledError,),
) -> None:
    """Cancel a detached task and wait for it to finish."""
    if task is None:
        return
    task.cancel()
    with suppress(*suppress_exceptions):
        await task


def _log_detached_task_result(task: asyncio.Task, *, message: str) -> None:
    """Log failures from a detached background task."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception(message)


def _create_logged_task(coro: Coroutine[Any, Any, None], *, name: str, failure_message: str) -> asyncio.Task:
    """Create a detached task that logs failures on completion."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(partial(_log_detached_task_result, message=failure_message))
    return task


async def _run_with_retry(
    step_name: str,
    operation: Callable[[], Awaitable[None]],
    *,
    initial_delay_seconds: float = _STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    max_delay_seconds: float = _STARTUP_RETRY_MAX_DELAY_SECONDS,
    permanent_error_check: Callable[[Exception], bool] | None = None,
    update_runtime_state: bool = True,
) -> None:
    """Run an async startup step until it succeeds or a permanent error occurs."""
    attempt = 0
    while True:
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if permanent_error_check is not None and permanent_error_check(exc):
                logger.exception("%s failed with a permanent error", step_name)
                raise
            attempt += 1
            retry_in_seconds = _retry_delay_seconds(
                attempt,
                initial_delay_seconds=initial_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            )
            logger.warning(
                "%s failed; retrying",
                step_name,
                attempt=attempt,
                retry_in_seconds=retry_in_seconds,
                exc_info=True,
            )
            if update_runtime_state:
                set_runtime_starting(f"{step_name} failed; retrying in {retry_in_seconds:.0f}s")
            await asyncio.sleep(retry_in_seconds)
        else:
            return


async def _wait_for_matrix_homeserver(
    *,
    timeout_seconds: float | None = None,
    request_timeout_seconds: float = _MATRIX_HOMESERVER_REQUEST_TIMEOUT_SECONDS,
    retry_interval_seconds: float = _MATRIX_HOMESERVER_RETRY_INTERVAL_SECONDS,
) -> None:
    """Wait for the configured Matrix homeserver to answer `/versions`."""
    if timeout_seconds is None:
        timeout_seconds = _matrix_homeserver_startup_timeout_seconds_from_env()
    versions_url = matrix_versions_url(MATRIX_HOMESERVER)
    set_runtime_starting(f"Waiting for Matrix homeserver at {versions_url}")
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    attempt = 0
    logger.info(
        "Waiting for Matrix homeserver",
        url=versions_url,
        timeout_seconds=timeout_seconds,
    )

    async with httpx.AsyncClient(timeout=request_timeout_seconds, verify=MATRIX_SSL_VERIFY) as client:
        while deadline is None or loop.time() < deadline:
            attempt += 1
            try:
                response = await client.get(versions_url)
            except httpx.TransportError as exc:
                if attempt == 1 or attempt % 5 == 0:
                    logger.info(
                        "Matrix homeserver not ready yet",
                        url=versions_url,
                        attempt=attempt,
                        error=str(exc),
                    )
                await asyncio.sleep(retry_interval_seconds)
                continue

            if response_has_matrix_versions(response):
                logger.info("Matrix homeserver ready", url=versions_url)
                return

            if attempt == 1 or attempt % 5 == 0:
                logger.info(
                    "Matrix homeserver not ready yet",
                    url=versions_url,
                    attempt=attempt,
                    status_code=response.status_code,
                    body_preview=response.text[:200].replace("\n", " "),
                )
            await asyncio.sleep(retry_interval_seconds)

    msg = f"Timed out waiting for Matrix homeserver at {versions_url}"
    raise RuntimeError(msg)


@dataclass(slots=True)
class _EntityStartResults:
    """Result of one pass trying to start a batch of entities."""

    started_bots: list[AgentBot | TeamBot] = field(default_factory=list)
    retryable_entities: list[str] = field(default_factory=list)
    permanently_failed_entities: list[str] = field(default_factory=list)


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
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
        Skipped when mindroom_user is not configured (e.g. hosted/public profile).
        """
        if config.mindroom_user is None:
            logger.debug("mindroom_user not configured, skipping user account creation")
            return
        # The user account is just another "agent" from the perspective of account management
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

    async def _prepare_user_account(self, config: Config, *, update_runtime_state: bool) -> None:
        """Ensure the internal user account exists, retrying only transient failures."""
        await _run_with_retry(
            "Preparing MindRoom user account",
            lambda: self._ensure_user_account(config),
            permanent_error_check=_is_permanent_startup_error,
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
        await _cancel_task(task, suppress_exceptions=(asyncio.CancelledError, Exception))

    async def _cancel_bot_start_task(self, entity_name: str) -> None:
        """Cancel any background start task for one bot."""
        task = self._bot_start_tasks.pop(entity_name, None)
        await _cancel_task(task)

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
            _sync_forever_with_restart(bot),
            name=f"sync_{entity_name}",
        )

    @staticmethod
    def _log_permanent_bot_start_failure(entity_name: str) -> None:
        """Log that one bot failed in a non-retryable way and stays disabled."""
        logger.error(
            "Bot startup failed permanently; leaving bot disabled until configuration changes",
            agent_name=entity_name,
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

    async def _try_start_bot_once(
        self,
        entity_name: str,
        bot: AgentBot | TeamBot,
    ) -> bool | None:
        """Run one bot start attempt and classify the result."""
        try:
            return bool(await bot.try_start())
        except PermanentMatrixStartupError:
            self._log_permanent_bot_start_failure(entity_name)
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
                        await _run_with_retry(
                            f"Updating Matrix room memberships for {entity_name}",
                            partial(self._setup_rooms_and_memberships, bots_to_setup),
                            update_runtime_state=False,
                        )
                    self._start_sync_task(entity_name, bot)
                    return

                attempt += 1
                retry_in_seconds = _retry_delay_seconds(
                    attempt,
                    initial_delay_seconds=_STARTUP_RETRY_INITIAL_DELAY_SECONDS,
                    max_delay_seconds=_STARTUP_RETRY_MAX_DELAY_SECONDS,
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
        self._bot_start_tasks[entity_name] = _create_logged_task(
            self._run_bot_start_retry(entity_name),
            name=f"retry_start_{entity_name}",
            failure_message="Background bot start task failed",
        )

    async def _run_knowledge_refresh(self, config: Config, *, start_watcher: bool) -> None:
        """Run background knowledge refresh until it succeeds or is cancelled."""
        current_task = asyncio.current_task()
        try:
            await _run_with_retry(
                "Background knowledge refresh",
                lambda: self._configure_knowledge(config, start_watcher=start_watcher),
                update_runtime_state=False,
            )
        finally:
            if self._knowledge_refresh_task is current_task:
                self._knowledge_refresh_task = None

    async def _schedule_knowledge_refresh(self, config: Config, *, start_watcher: bool) -> None:
        """Schedule knowledge refresh in the background, replacing any in-flight run."""
        await self._cancel_knowledge_refresh_task()
        self._knowledge_refresh_task = _create_logged_task(
            self._run_knowledge_refresh(config, start_watcher=start_watcher),
            name="knowledge_refresh",
            failure_message="Background knowledge refresh failed",
        )

    async def _refresh_knowledge_for_runtime(self, config: Config, *, start_watcher: bool) -> None:
        """Refresh knowledge now (startup path) or in background (runtime updates)."""
        if self.running:
            await self._schedule_knowledge_refresh(config, start_watcher=start_watcher)
            return
        await self._configure_knowledge(config, start_watcher=start_watcher)

    async def _sync_runtime_support_services(self, config: Config, *, start_watcher: bool) -> None:
        """Refresh runtime support services that depend on the active config."""
        await self._refresh_knowledge_for_runtime(config, start_watcher=start_watcher)
        await self._sync_memory_auto_flush_worker()

    @staticmethod
    def _configured_entity_names(config: Config) -> list[str]:
        """Return configured entity names with the router first."""
        return [ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys()]

    def _create_managed_bot(self, entity_name: str, config: Config) -> AgentBot | TeamBot | None:
        """Create and register one runtime-managed bot."""
        temp_user = _create_temp_user(entity_name, config)
        bot = create_bot_for_entity(entity_name, temp_user, config, self.storage_path)
        if bot is None:
            logger.warning(f"Could not create bot for {entity_name}")
            return None
        bot.orchestrator = self
        self.agent_bots[entity_name] = bot
        return bot

    async def _start_entities_once(
        self,
        entity_names: Iterable[str],
        *,
        start_sync_tasks: bool,
    ) -> _EntityStartResults:
        """Try to start each named entity once and classify the results."""
        entity_bots: list[tuple[str, AgentBot | TeamBot]] = []
        for entity_name in entity_names:
            bot = self.agent_bots.get(entity_name)
            if bot is not None:
                entity_bots.append((entity_name, bot))

        results = _EntityStartResults()
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
    ) -> _EntityStartResults:
        """Create configured entities and try to start them once."""
        created_entities = [
            entity_name for entity_name in entity_names if self._create_managed_bot(entity_name, config) is not None
        ]
        return await self._start_entities_once(created_entities, start_sync_tasks=start_sync_tasks)

    async def initialize(self) -> None:
        """Initialize all agent bots with self-management.

        Each agent is now responsible for ensuring its own user account and rooms.
        """
        set_runtime_starting("Loading config and preparing agents")
        logger.info("Initializing multi-agent system...")

        config = Config.from_yaml()
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
        await _run_with_retry(
            "Starting router Matrix account",
            _start_router,
            permanent_error_check=_is_permanent_startup_error,
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
        await _wait_for_matrix_homeserver()
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
        # knowledge indexing, so new rooms/invites are not delayed by embeddings.
        await _run_with_retry(
            "Setting up Matrix rooms and memberships",
            lambda: self._setup_rooms_and_memberships(started_bots),
        )

        self.running = True

        # Knowledge is optional for initial availability.
        set_runtime_starting("Refreshing knowledge bases in background")
        await self._schedule_knowledge_refresh(config, start_watcher=True)

        set_runtime_starting("Starting background workers")
        await self._sync_memory_auto_flush_worker()

        # Create sync tasks for each bot with automatic restart on failure
        set_runtime_starting("Starting Matrix sync loops")
        for entity_name, bot in self.agent_bots.items():
            if bot.running:
                self._start_sync_task(entity_name, bot)

        for entity_name in start_results.retryable_entities:
            await self._schedule_bot_start_retry(entity_name)

        set_runtime_ready()

        # Run all sync tasks
        await asyncio.gather(*tuple(self._sync_tasks.values()))

    async def update_config(self) -> bool:  # noqa: C901, PLR0912
        """Update configuration with simplified self-managing agents.

        Each agent handles its own user account creation and room management.

        Returns:
            True if any agents were updated, False otherwise.

        """
        new_config = Config.from_yaml()
        load_plugins(new_config)

        if not self.config:
            await self._prepare_user_account(new_config, update_runtime_state=not self.running)
            self.config = new_config
            await self._sync_runtime_support_services(new_config, start_watcher=self.running)
            return False

        current_config = self.config

        # Identify what changed - we can keep using the existing helper functions
        entities_to_restart = await _identify_entities_to_restart(current_config, new_config, self.agent_bots)
        mindroom_user_changed = current_config.mindroom_user != new_config.mindroom_user
        matrix_room_access_changed = current_config.matrix_room_access != new_config.matrix_room_access
        authorization_changed = current_config.authorization != new_config.authorization

        # Also check for new entities that didn't exist before
        all_new_entities = set(self._configured_entity_names(new_config))
        existing_entities = set(self.agent_bots.keys())
        new_entities = all_new_entities - existing_entities - entities_to_restart

        if mindroom_user_changed:
            await self._prepare_user_account(new_config, update_runtime_state=not self.running)

        # Only apply the new config after all validation/account checks succeed.
        self.config = new_config

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
            and not authorization_changed
        ):
            await self._sync_runtime_support_services(new_config, start_watcher=self.running)
            # No entities to restart or create, we're done
            return False

        # Stop entities that need restarting
        if entities_to_restart:
            for entity_name in entities_to_restart:
                await self._cancel_bot_start_task(entity_name)
            await _stop_entities(entities_to_restart, self.agent_bots, self._sync_tasks)

        entities_to_recreate = entities_to_restart & all_new_entities
        removed_restarted_entities = entities_to_restart - all_new_entities
        changed_entities = entities_to_recreate | new_entities
        start_results = await self._create_and_start_entities(
            changed_entities,
            new_config,
            start_sync_tasks=True,
        )

        for entity_name in removed_restarted_entities:
            self.agent_bots.pop(entity_name, None)

        # Handle removed entities (cleanup)
        removed_entities = existing_entities - all_new_entities
        for entity_name in removed_entities:
            await self._cancel_bot_start_task(entity_name)
            # Cancel sync task first
            await _cancel_sync_task(entity_name, self._sync_tasks)

            if entity_name in self.agent_bots:
                bot = self.agent_bots[entity_name]
                await bot.cleanup()  # Agent handles its own cleanup
                del self.agent_bots[entity_name]

        # Setup rooms and have new/restarted bots join them
        bots_to_setup = self._running_bots_for_entities(changed_entities)

        if bots_to_setup or mindroom_user_changed or matrix_room_access_changed or authorization_changed:
            await self._setup_rooms_and_memberships(bots_to_setup)

        for entity_name in start_results.retryable_entities:
            await self._schedule_bot_start_retry(entity_name)

        if start_results.permanently_failed_entities:
            logger.warning(
                "Configuration update left some bots disabled due to permanent startup errors",
                agent_names=start_results.permanently_failed_entities,
            )

        await self._sync_runtime_support_services(new_config, start_watcher=self.running)

        logger.info(f"Configuration update complete: {len(entities_to_restart) + len(new_entities)} bots affected")
        return True

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        await self._stop_memory_auto_flush_worker()
        await self._cancel_knowledge_refresh_task()
        await self._cancel_bot_start_tasks()
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
        config = self._require_config()
        for bot in bots:
            # Get the room aliases for this entity from config and resolve to IDs
            room_aliases = get_rooms_for_entity(bot.agent_name, config)
            bot.rooms = resolve_room_aliases(room_aliases)

        async def _ensure_internal_user_memberships() -> None:
            all_rooms = load_rooms()
            all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
            if all_room_ids and config.mindroom_user is not None:
                await ensure_user_in_rooms(MATRIX_HOMESERVER, all_room_ids)

        # First invitation/join pass for rooms the router is already in.
        await self._ensure_room_invitations()
        await _ensure_internal_user_memberships()
        await asyncio.gather(*(bot.ensure_rooms() for bot in bots))

        # Existing invite-only rooms may resolve before the router is a member.
        # Rerun room reconciliation after the router's first join pass so topic
        # and access policy updates apply once the router can manage the room.
        if any(bot.agent_name == ROUTER_AGENT_NAME for bot in bots):
            await self._ensure_rooms_exist()

        # Existing invite-only rooms may only become joinable for others after the
        # router joins them in the first pass, so retry invitations once more.
        await self._ensure_room_invitations()
        await _ensure_internal_user_memberships()

        follow_up_bots = [bot for bot in bots if bot.agent_name != ROUTER_AGENT_NAME]
        if follow_up_bots:
            await asyncio.gather(*(bot.ensure_rooms() for bot in follow_up_bots))

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
        config = self._require_config()
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
        if config.mindroom_user is not None and user_account:
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

        agents_differ = _config_entries_differ(old_agent, new_agent)

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
    task = sync_tasks.pop(entity_name, None)
    await _cancel_task(task)


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

            # Wait a bit before restarting to avoid rapid restarts.
            wait_time = min(60, 5 * retry_count)  # Linear backoff, max 60 seconds
            logger.info(f"Restarting sync loop for {bot.agent_name} in {wait_time} seconds...")
            await asyncio.sleep(wait_time)


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
    """Run the bundled dashboard/API server as an asyncio task."""
    from mindroom.api.main import app as api_app  # noqa: PLC0415  # avoid heavy import at module level

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
            _retry_delay_seconds(
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
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
        storage_path: The base directory for storing agent data
        api: Whether to start the bundled dashboard/API server
        api_port: Port for the bundled dashboard/API server
        api_host: Host for the bundled dashboard/API server

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
    set_runtime_starting()
    auxiliary_tasks: list[asyncio.Task] = []

    try:
        auxiliary_specs = [
            ("config watcher", lambda: _watch_config_task(config_path, orchestrator), "config_watcher_supervisor"),
            ("skills watcher", lambda: _watch_skills_task(orchestrator), "skills_watcher_supervisor"),
        ]

        # Optionally start the bundled dashboard/API server
        if api:
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
        for task in auxiliary_tasks:
            task.cancel()
        for task in auxiliary_tasks:
            with suppress(asyncio.CancelledError):
                await task
        # Final cleanup
        await orchestrator.stop()
        reset_runtime_state()
