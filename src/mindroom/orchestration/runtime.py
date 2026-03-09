"""Pure runtime helpers for the multi-agent orchestrator."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.constants import MATRIX_HOMESERVER, MATRIX_SSL_VERIFY, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.health import matrix_versions_url, response_has_matrix_versions
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
