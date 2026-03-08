"""Runtime lifecycle helpers for the multi-agent orchestrator."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.main import Config
from mindroom.constants import MATRIX_HOMESERVER, MATRIX_SSL_VERIFY, ROUTER_AGENT_NAME
from mindroom.knowledge.manager import initialize_knowledge_managers, shutdown_knowledge_managers
from mindroom.logging_config import get_logger
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.health import matrix_versions_url, response_has_matrix_versions
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
)
from mindroom.memory.auto_flush import MemoryAutoFlushWorker, auto_flush_enabled
from mindroom.runtime_state import set_runtime_failed, set_runtime_ready, set_runtime_starting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.orchestrator import MultiAgentOrchestrator

logger = get_logger(__name__)

_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV = "MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS"
_MATRIX_HOMESERVER_REQUEST_TIMEOUT_SECONDS = 5.0
_MATRIX_HOMESERVER_RETRY_INTERVAL_SECONDS = 2.0
_STARTUP_RETRY_INITIAL_DELAY_SECONDS = 2.0
_STARTUP_RETRY_MAX_DELAY_SECONDS = 60.0


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


def _create_logged_task(
    coro: Coroutine[Any, Any, None],
    *,
    name: str,
    failure_message: str,
) -> asyncio.Task:
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
    raise TimeoutError(msg)


@dataclass
class _EntityStartResults:
    """Classification of one batch of entity start attempts."""

    started_bots: list[AgentBot | TeamBot]
    retryable_entities: list[str]
    permanently_failed_entities: list[str]

    def __init__(self) -> None:
        self.started_bots = []
        self.retryable_entities = []
        self.permanently_failed_entities = []


async def _stop_memory_auto_flush_worker(self: MultiAgentOrchestrator) -> None:
    """Stop the background memory auto-flush worker if running."""
    worker = self._memory_auto_flush_worker
    task = self._memory_auto_flush_task
    self._memory_auto_flush_worker = None
    self._memory_auto_flush_task = None

    if worker is not None:
        worker.stop()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def _sync_memory_auto_flush_worker(self: MultiAgentOrchestrator) -> None:
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


async def _ensure_user_account(self: MultiAgentOrchestrator, config: Config) -> None:
    """Ensure the internal user account exists when configured."""
    if config.mindroom_user is None:
        logger.debug("mindroom_user not configured, skipping user account creation")
        return
    user_account = await self.create_agent_user(
        self.matrix_homeserver,
        INTERNAL_USER_AGENT_NAME,
        config.mindroom_user.display_name,
        username=config.mindroom_user.username,
    )
    logger.info(f"User account ready: {user_account.user_id}")


def _require_config(self: MultiAgentOrchestrator) -> Config:
    """Return the active config or fail fast if it has not been loaded."""
    config = self.config
    if config is None:
        msg = "Configuration not loaded"
        raise RuntimeError(msg)
    return config


async def _prepare_user_account(
    self: MultiAgentOrchestrator,
    config: Config,
    *,
    update_runtime_state: bool,
) -> None:
    """Ensure the internal user account exists, retrying only transient failures."""
    await _run_with_retry(
        "Preparing MindRoom user account",
        lambda: self._ensure_user_account(config),
        permanent_error_check=_is_permanent_startup_error,
        update_runtime_state=update_runtime_state,
    )


async def _configure_knowledge(self: MultiAgentOrchestrator, config: Config, *, start_watcher: bool) -> None:
    """Initialize or reconfigure knowledge managers for the current config."""
    self.knowledge_managers = await initialize_knowledge_managers(
        config=config,
        storage_path=self.storage_path,
        start_watchers=start_watcher,
        reindex_on_create=False,
    )


async def _cancel_knowledge_refresh_task(self: MultiAgentOrchestrator) -> None:
    """Cancel any in-flight background knowledge refresh task."""
    task = self._knowledge_refresh_task
    self._knowledge_refresh_task = None
    await _cancel_task(task, suppress_exceptions=(asyncio.CancelledError, Exception))


async def _cancel_bot_start_task(self: MultiAgentOrchestrator, entity_name: str) -> None:
    """Cancel any background start task for one bot."""
    task = self._bot_start_tasks.pop(entity_name, None)
    await _cancel_task(task)


async def _cancel_bot_start_tasks(self: MultiAgentOrchestrator) -> None:
    """Cancel all background bot start tasks."""
    for entity_name in tuple(self._bot_start_tasks):
        await self._cancel_bot_start_task(entity_name)


def _start_sync_task(self: MultiAgentOrchestrator, entity_name: str, bot: AgentBot | TeamBot) -> None:
    """Ensure one sync task exists for a running bot."""
    existing_task = self._sync_tasks.get(entity_name)
    if existing_task is not None and not existing_task.done():
        return
    self._sync_tasks[entity_name] = asyncio.create_task(
        self.sync_forever_with_restart(bot),
        name=f"sync_{entity_name}",
    )


def _log_permanent_bot_start_failure(entity_name: str) -> None:
    """Log that one bot failed in a non-retryable way and stays disabled."""
    logger.error(
        "Bot startup failed permanently; leaving bot disabled until configuration changes",
        agent_name=entity_name,
    )


def _bots_to_setup_after_background_start(
    self: MultiAgentOrchestrator,
    entity_name: str,
) -> list[AgentBot | TeamBot]:
    """Return the bots whose room memberships should be reconciled after a background start."""
    if entity_name == ROUTER_AGENT_NAME:
        return self._running_bots_for_entities(self.agent_bots)
    return self._running_bots_for_entities((entity_name,))


def _running_bots_for_entities(
    self: MultiAgentOrchestrator,
    entity_names: Iterable[str],
) -> list[AgentBot | TeamBot]:
    """Return running bots for the given entity names."""
    running_bots: list[AgentBot | TeamBot] = []
    for entity_name in entity_names:
        bot = self.agent_bots.get(entity_name)
        if bot is not None and bot.running:
            running_bots.append(bot)
    return running_bots


async def _try_start_bot_once(
    _orchestrator: MultiAgentOrchestrator,
    entity_name: str,
    bot: AgentBot | TeamBot,
) -> bool | None:
    """Run one bot start attempt and classify the result."""
    try:
        return bool(await bot.try_start())
    except PermanentMatrixStartupError:
        _log_permanent_bot_start_failure(entity_name)
        return None


async def _run_bot_start_retry(self: MultiAgentOrchestrator, entity_name: str) -> None:
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


async def _schedule_bot_start_retry(self: MultiAgentOrchestrator, entity_name: str) -> None:
    """Schedule background retries for one failed bot startup."""
    await self._cancel_bot_start_task(entity_name)
    self._bot_start_tasks[entity_name] = _create_logged_task(
        self._run_bot_start_retry(entity_name),
        name=f"retry_start_{entity_name}",
        failure_message="Background bot start task failed",
    )


async def _run_knowledge_refresh(
    self: MultiAgentOrchestrator,
    config: Config,
    *,
    start_watcher: bool,
) -> None:
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


async def _schedule_knowledge_refresh(
    self: MultiAgentOrchestrator,
    config: Config,
    *,
    start_watcher: bool,
) -> None:
    """Schedule knowledge refresh in the background, replacing any in-flight run."""
    await self._cancel_knowledge_refresh_task()
    self._knowledge_refresh_task = _create_logged_task(
        self._run_knowledge_refresh(config, start_watcher=start_watcher),
        name="knowledge_refresh",
        failure_message="Background knowledge refresh failed",
    )


async def _refresh_knowledge_for_runtime(
    self: MultiAgentOrchestrator,
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
    self: MultiAgentOrchestrator,
    config: Config,
    *,
    start_watcher: bool,
) -> None:
    """Refresh runtime support services that depend on the active config."""
    await self._refresh_knowledge_for_runtime(config, start_watcher=start_watcher)
    await self._sync_memory_auto_flush_worker()


def _configured_entity_names(config: Config) -> list[str]:
    """Return configured entity names with the router first."""
    return [ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys()]


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
        user_id="",
        display_name=display_name,
        password="",
    )


def _create_managed_bot(
    self: MultiAgentOrchestrator,
    entity_name: str,
    config: Config,
) -> AgentBot | TeamBot | None:
    """Create and register one runtime-managed bot."""
    temp_user = _create_temp_user(entity_name, config)
    bot = self.bot_factory(entity_name, temp_user, config, self.storage_path)
    bot.orchestrator = self
    self.agent_bots[entity_name] = bot
    return bot


async def _start_entities_once(
    self: MultiAgentOrchestrator,
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
    self: MultiAgentOrchestrator,
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


async def initialize(self: MultiAgentOrchestrator) -> None:
    """Initialize all managed bots from configuration."""
    set_runtime_starting("Loading config and preparing agents")
    logger.info("Initializing multi-agent system...")

    config = Config.from_yaml()
    self.load_plugins(config)
    await self._prepare_user_account(config, update_runtime_state=True)
    self.config = config
    for entity_name in _configured_entity_names(config):
        self._create_managed_bot(entity_name, config)

    logger.info("Initialized agent bots", count=len(self.agent_bots))


async def start(self: MultiAgentOrchestrator) -> None:
    """Start all agent bots and publish readiness state."""
    try:
        await self._start_runtime()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        set_runtime_failed(str(exc))
        raise


async def _start_router_bot(self: MultiAgentOrchestrator) -> AgentBot | TeamBot:
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


def _log_degraded_startup(self: MultiAgentOrchestrator, failed_agents: list[str]) -> None:
    """Log degraded startup status for failed non-router bots."""
    if failed_agents:
        logger.warning(
            f"System starting in degraded mode. "
            f"Failed agents: {', '.join(failed_agents)} "
            f"({len(self.agent_bots) - len(failed_agents)}/{len(self.agent_bots)} operational)",
        )
        return
    logger.info("All agent bots started successfully")


async def _start_runtime(self: MultiAgentOrchestrator) -> None:
    """Run the startup sequence before handing off to the sync loops."""
    await self.wait_for_matrix_homeserver()
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

    await _run_with_retry(
        "Setting up Matrix rooms and memberships",
        lambda: self._setup_rooms_and_memberships(started_bots),
    )

    self.running = True

    set_runtime_starting("Refreshing knowledge bases in background")
    await self._schedule_knowledge_refresh(config, start_watcher=True)

    set_runtime_starting("Starting background workers")
    await self._sync_memory_auto_flush_worker()

    set_runtime_starting("Starting Matrix sync loops")
    for entity_name, bot in self.agent_bots.items():
        if bot.running:
            self._start_sync_task(entity_name, bot)

    for entity_name in start_results.retryable_entities:
        await self._schedule_bot_start_retry(entity_name)

    set_runtime_ready()
    await asyncio.gather(*tuple(self._sync_tasks.values()))


async def _cancel_sync_task(entity_name: str, sync_tasks: dict[str, asyncio.Task]) -> None:
    """Cancel and remove a sync task for an entity."""
    task = sync_tasks.pop(entity_name, None)
    await _cancel_task(task)


async def _stop_entities(
    entities_to_restart: set[str],
    agent_bots: dict[str, Any],
    sync_tasks: dict[str, asyncio.Task],
) -> None:
    """Stop a set of entities and remove them from runtime maps."""
    for entity_name in entities_to_restart:
        await _cancel_sync_task(entity_name, sync_tasks)

    stop_tasks = [agent_bots[entity_name].stop() for entity_name in entities_to_restart if entity_name in agent_bots]

    if stop_tasks:
        await asyncio.gather(*stop_tasks)

    for entity_name in entities_to_restart:
        agent_bots.pop(entity_name, None)


async def _sync_forever_with_restart(bot: AgentBot | TeamBot, max_retries: int = -1) -> None:
    """Run sync_forever with automatic restart on failure."""
    retry_count = 0
    while bot.running and (max_retries < 0 or retry_count < max_retries):
        try:
            logger.info(f"Starting sync loop for {bot.agent_name}")
            await bot.sync_forever()
            break
        except asyncio.CancelledError:
            logger.info(f"Sync task for {bot.agent_name} was cancelled")
            break
        except Exception:
            retry_count += 1
            logger.exception(f"Sync loop failed for {bot.agent_name} (retry {retry_count})")

            if not bot.running:
                break

            if max_retries >= 0 and retry_count >= max_retries:
                logger.exception(f"Max retries ({max_retries}) reached for {bot.agent_name}, giving up")
                break

            wait_time = min(60, 5 * retry_count)
            logger.info(f"Restarting sync loop for {bot.agent_name} in {wait_time} seconds...")
            await asyncio.sleep(wait_time)


async def stop(self: MultiAgentOrchestrator) -> None:
    """Stop all agent bots."""
    self.running = False
    await self._stop_memory_auto_flush_worker()
    await self._cancel_knowledge_refresh_task()
    await self._cancel_bot_start_tasks()
    await shutdown_knowledge_managers()
    self.knowledge_managers = {}

    for entity_name in list(self._sync_tasks.keys()):
        await self.cancel_sync_task(entity_name, self._sync_tasks)

    for bot in self.agent_bots.values():
        bot.running = False

    stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
    await asyncio.gather(*stop_tasks)
    logger.info("All agent bots stopped")
