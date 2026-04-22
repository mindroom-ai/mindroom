"""Pure runtime helpers for the multi-agent orchestrator."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom import constants
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, request_task_cancel, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.client_session import PermanentMatrixStartupError
from mindroom.matrix.health import (
    MATRIX_SYNC_STARTUP_GRACE_SECONDS,
    MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS,
    matrix_versions_url,
    response_has_matrix_versions,
)
from mindroom.matrix.users import AgentMatrixUser
from mindroom.runtime_state import set_runtime_starting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)

_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV = "MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS"
_MATRIX_HOMESERVER_REQUEST_TIMEOUT_SECONDS = 5.0
_MATRIX_HOMESERVER_RETRY_INTERVAL_SECONDS = 2.0
STARTUP_RETRY_INITIAL_DELAY_SECONDS = 2.0
STARTUP_RETRY_MAX_DELAY_SECONDS = 60.0
_CANCELLING_LOGGED_TASKS: set[asyncio.Task[Any]] = set()
_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS = 5.0
_MATRIX_SYNC_STARTUP_TIMEOUT_ENV = "MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS"
USER_STOP_CANCEL_MSG = "user_stop"
SYNC_RESTART_CANCEL_MSG = "sync_restart"

CancelSource = Literal["user_stop", "sync_restart", "interrupted"]


def classify_cancel_source(exc: asyncio.CancelledError) -> CancelSource:
    """Return the visible cancellation provenance for one CancelledError."""
    if len(exc.args) == 0:
        return "interrupted"
    if exc.args[0] == USER_STOP_CANCEL_MSG:
        return "user_stop"
    if exc.args[0] == SYNC_RESTART_CANCEL_MSG:
        return "sync_restart"
    return "interrupted"


def is_sync_restart_cancel(exc: asyncio.CancelledError) -> bool:
    """Return whether one cancellation was caused by a sync restart."""
    return classify_cancel_source(exc) == "sync_restart"


def matrix_sync_startup_timeout_seconds(runtime_paths: RuntimePaths) -> float:
    """Return the sync startup timeout resolved via ``RuntimePaths``."""
    raw = (runtime_paths.env_value(_MATRIX_SYNC_STARTUP_TIMEOUT_ENV) or "").strip()
    if not raw:
        return MATRIX_SYNC_STARTUP_GRACE_SECONDS
    value = float(raw)
    if value <= 0:
        msg = f"{_MATRIX_SYNC_STARTUP_TIMEOUT_ENV} must be a positive number"
        raise ValueError(msg)
    return value


def _matrix_homeserver_startup_timeout_seconds_from_env(
    runtime_paths: RuntimePaths,
) -> int | None:
    """Return the startup wait timeout from the environment, if configured."""
    raw_timeout = (runtime_paths.env_value(_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV) or "").strip()
    if not raw_timeout:
        return None
    timeout_seconds = int(raw_timeout)
    if timeout_seconds == 0:
        return None
    if timeout_seconds < 0:
        msg = f"{_MATRIX_HOMESERVER_STARTUP_TIMEOUT_ENV} must be 0 or a positive integer"
        raise ValueError(msg)
    return timeout_seconds


def retry_delay_seconds(
    attempt: int,
    *,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> float:
    """Return capped exponential backoff delay for a retry attempt."""
    exponent = max(0, attempt - 1)
    return min(max_delay_seconds, initial_delay_seconds * (2**exponent))


def is_permanent_startup_error(exc: Exception) -> bool:
    """Return whether a startup exception is clearly non-retryable."""
    return isinstance(exc, PermanentMatrixStartupError)


async def cancel_task(
    task: asyncio.Task | None,
    *,
    suppress_exceptions: tuple[type[BaseException], ...] = (asyncio.CancelledError,),
    cancel_msg: str | None = None,
) -> None:
    """Cancel a detached task and wait for it to finish."""
    if task is None:
        return
    request_task_cancel(task, cancel_msg=cancel_msg)
    with suppress(*suppress_exceptions):
        await task


async def cancel_logged_task(task: asyncio.Task | None) -> None:
    """Cancel a detached logged task without re-raising its completion exception."""
    if task is None:
        return
    _CANCELLING_LOGGED_TASKS.add(task)
    try:
        await cancel_task(task)
    except Exception:
        # `_log_detached_task_result` downgrades these cancellation-time failures to
        # debug logging, so shutdown/reload should not re-raise them here.
        return


class MatrixSyncStalledError(RuntimeError):
    """Raised when the watchdog detects a stalled Matrix sync loop."""


@dataclass(slots=True)
class _SyncIteration:
    """Own the lifecycle of one sync task and its watchdog."""

    bot: AgentBot | TeamBot
    sync_task: asyncio.Task[Any] | None
    watchdog_task: asyncio.Task[Any] | None

    @staticmethod
    async def _watch(bot: AgentBot | TeamBot, sync_task: asyncio.Task[Any]) -> None:
        """Cancel a sync task when it stops reporting successful sync responses.

        Before the first ``SyncResponse`` (or ``SyncError``) arrives, the monotonic
        watchdog clock is not armed (``seconds_since_last_sync_activity`` returns
        ``None``). During that startup window a separate, longer timeout protects
        against a first sync that never completes.
        """
        startup_timeout_seconds = matrix_sync_startup_timeout_seconds(bot.runtime_paths)
        startup_monotonic = time.monotonic()
        while bot.running and not sync_task.done():
            await asyncio.sleep(_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS)
            sync_age_seconds = bot.seconds_since_last_sync_activity()

            if sync_age_seconds is None:
                # Still waiting for the first SyncResponse/SyncError.
                elapsed = time.monotonic() - startup_monotonic
                if elapsed <= startup_timeout_seconds:
                    continue
                logger.error(
                    "Matrix sync watchdog: first sync never completed",
                    agent_name=bot.agent_name,
                    elapsed_seconds=elapsed,
                    startup_timeout_seconds=startup_timeout_seconds,
                )
            elif sync_age_seconds <= MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS:
                continue
            else:
                logger.error(
                    "Matrix sync watchdog detected a stalled sync loop",
                    agent_name=bot.agent_name,
                    stale_for_seconds=sync_age_seconds,
                    last_sync_time=bot.last_sync_time.isoformat() if bot.last_sync_time is not None else None,
                )

            request_task_cancel(sync_task, cancel_msg=SYNC_RESTART_CANCEL_MSG)
            with suppress(asyncio.CancelledError):
                await sync_task
            msg = f"Matrix sync loop stalled for {bot.agent_name}"
            raise MatrixSyncStalledError(msg)

    @classmethod
    def start(cls, bot: AgentBot | TeamBot) -> _SyncIteration:
        """Create the sync task and its watchdog for one loop iteration."""
        bot.mark_sync_loop_started()
        # Reset the monotonic watchdog clock so a fresh iteration gets the full
        # startup timeout instead of inheriting a stale timestamp from a previous
        # stall/restart cycle.
        bot.reset_watchdog_clock()
        sync_task = asyncio.create_task(bot.sync_forever(), name=f"matrix_sync_{bot.agent_name}")
        watchdog_coro = cls._watch(bot, sync_task)
        try:
            watchdog_task = asyncio.create_task(
                watchdog_coro,
                name=f"matrix_sync_watchdog_{bot.agent_name}",
            )
        except BaseException:
            watchdog_coro.close()
            sync_task.cancel()
            raise
        return cls(bot=bot, sync_task=sync_task, watchdog_task=watchdog_task)

    async def wait(self) -> None:
        """Wait for the first task to finish and surface the real failure."""
        if self.sync_task is None or self.watchdog_task is None:
            return
        done, _ = await asyncio.wait(
            {self.sync_task, self.watchdog_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self.sync_task in done:
            if not self.sync_task.cancelled():
                await self.sync_task  # raises if sync_forever failed; returns if clean
                return
            # Let the watchdog surface its stalled-sync error when it initiated the
            # cancellation; otherwise preserve the original sync-task cancellation.
            await self.watchdog_task
            await self.sync_task
            return
        await self.watchdog_task

    async def cancel(self) -> None:
        """Cancel child tasks without masking the original failure."""
        for attr in ("watchdog_task", "sync_task"):
            task = getattr(self, attr)
            if task is None:
                continue
            setattr(self, attr, None)
            if attr == "sync_task":
                request_task_cancel(task, cancel_msg=SYNC_RESTART_CANCEL_MSG)
            else:
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, MatrixSyncStalledError):
                pass
            except Exception:
                logger.warning("Suppressed error during sync iteration cleanup", exc_info=True)


def _log_detached_task_result(task: asyncio.Task, *, message: str) -> None:
    """Log failures from a detached background task."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        if task in _CANCELLING_LOGGED_TASKS:
            logger.debug(
                "Detached task failed while being cancelled",
                task_name=task.get_name(),
                exc_info=True,
            )
            return
        logger.exception(message)
    finally:
        _CANCELLING_LOGGED_TASKS.discard(task)


def create_logged_task(
    coro: Coroutine[Any, Any, None],
    *,
    name: str,
    failure_message: str,
) -> asyncio.Task:
    """Create a detached task that logs failures on completion."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(partial(_log_detached_task_result, message=failure_message))
    return task


async def run_with_retry(
    step_name: str,
    operation: Callable[[], Awaitable[None]],
    *,
    initial_delay_seconds: float = STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    max_delay_seconds: float = STARTUP_RETRY_MAX_DELAY_SECONDS,
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
                logger.error("startup_step_failed_permanently", step_name=step_name, error=str(exc))  # noqa: TRY400
                raise
            attempt += 1
            retry_in_seconds = retry_delay_seconds(
                attempt,
                initial_delay_seconds=initial_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            )
            logger.warning(
                "startup_step_retrying",
                step_name=step_name,
                attempt=attempt,
                retry_in_seconds=retry_in_seconds,
                exc_info=True,
            )
            if update_runtime_state:
                set_runtime_starting(f"{step_name} failed; retrying in {retry_in_seconds:.0f}s")
            await asyncio.sleep(retry_in_seconds)
        else:
            return


async def wait_for_matrix_homeserver(
    *,
    runtime_paths: RuntimePaths,
    timeout_seconds: float | None = None,
    request_timeout_seconds: float = _MATRIX_HOMESERVER_REQUEST_TIMEOUT_SECONDS,
    retry_interval_seconds: float = _MATRIX_HOMESERVER_RETRY_INTERVAL_SECONDS,
) -> None:
    """Wait for the configured Matrix homeserver to answer `/versions`."""
    if timeout_seconds is None:
        timeout_seconds = _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths)
    versions_url = matrix_versions_url(constants.runtime_matrix_homeserver(runtime_paths=runtime_paths))
    set_runtime_starting(f"Waiting for Matrix homeserver at {versions_url}")
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    attempt = 0
    logger.info(
        "Waiting for Matrix homeserver",
        url=versions_url,
        timeout_seconds=timeout_seconds,
    )

    async with httpx.AsyncClient(
        timeout=request_timeout_seconds,
        verify=runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
    ) as client:
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


@dataclass(slots=True)
class EntityStartResults:
    """Result of one pass trying to start a batch of entities."""

    started_bots: list[AgentBot | TeamBot] = field(default_factory=list)
    retryable_entities: list[str] = field(default_factory=list)
    permanently_failed_entities: list[str] = field(default_factory=list)


def create_temp_user(entity_name: str, config: Config) -> AgentMatrixUser:
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
        user_id="",  # Populated later by ensure_user_account.
        display_name=display_name,
        password="",  # Populated later by ensure_user_account.
    )


async def cancel_sync_task(
    entity_name: str,
    sync_tasks: dict[str, asyncio.Task],
    *,
    cancel_msg: str | None = None,
) -> None:
    """Cancel and remove a sync task for an entity."""
    task = sync_tasks.pop(entity_name, None)
    await cancel_task(task, cancel_msg=cancel_msg)


async def stop_entities(
    entities_to_restart: set[str],
    agent_bots: dict[str, AgentBot | TeamBot],
    sync_tasks: dict[str, asyncio.Task],
) -> None:
    """Stop a set of entities and remove them from runtime maps."""
    # Cancel teardown-sensitive background work before stopping sync loops.
    for entity_name in entities_to_restart:
        bot = agent_bots.get(entity_name)
        if bot is not None:
            await bot.prepare_for_sync_shutdown()

    # Cancel sync tasks next so restarted entities do not accumulate duplicate loops.
    for entity_name in entities_to_restart:
        await cancel_sync_task(entity_name, sync_tasks, cancel_msg=SYNC_RESTART_CANCEL_MSG)

    stop_tasks = [
        agent_bots[entity_name].stop(reason="restart")
        for entity_name in entities_to_restart
        if entity_name in agent_bots
    ]
    if stop_tasks:
        await asyncio.gather(*stop_tasks)

    for entity_name in entities_to_restart:
        agent_bots.pop(entity_name, None)


async def sync_forever_with_restart(bot: AgentBot | TeamBot, max_retries: int = -1) -> None:
    """Run sync_forever with automatic restart on failure."""
    retry_count = 0
    while bot.running and (max_retries < 0 or retry_count < max_retries):
        iteration: _SyncIteration | None = None
        try:
            logger.info("starting_sync_loop", agent=bot.agent_name)
            iteration = _SyncIteration.start(bot)
            await iteration.wait()
            # sync_forever returned normally, so the bot was stopped intentionally.
            break
        except asyncio.CancelledError:
            # Task cancellation is part of normal shutdown.
            logger.info("sync_task_cancelled", agent=bot.agent_name)
            break
        except MatrixSyncStalledError:
            retry_count += 1
            logger.warning("restarting_stalled_sync_loop", agent=bot.agent_name, retry_count=retry_count)
        except Exception:
            retry_count += 1
            logger.exception("sync_loop_failed", agent=bot.agent_name, retry_count=retry_count)
        finally:
            if iteration is not None:
                await bot.prepare_for_sync_shutdown()
                await iteration.cancel()

        if not bot.running or (max_retries >= 0 and retry_count >= max_retries):
            break

        wait_time = retry_delay_seconds(
            retry_count,
            initial_delay_seconds=5.0,
            max_delay_seconds=60.0,
        )
        logger.info("restarting_sync_loop", agent=bot.agent_name, retry_count=retry_count, wait_seconds=wait_time)
        await asyncio.sleep(wait_time)
