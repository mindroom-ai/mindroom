"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar, cast
from uuid import uuid4

import nio
import uvicorn

from mindroom import constants
from mindroom.agents import ensure_default_agent_workspaces, get_rooms_for_entity
from mindroom.authorization import is_authorized_sender
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import configured_bot_usernames_for_room
from mindroom.hooks import (
    EVENT_CONFIG_RELOADED,
    ConfigReloadedContext,
    HookRegistry,
    build_hook_matrix_admin,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
)
from mindroom.knowledge import KnowledgeRefreshScheduler
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members, invite_to_room
from mindroom.matrix.client_session import PermanentMatrixStartupError
from mindroom.matrix.health import reset_matrix_sync_health
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.rooms import (
    ensure_all_rooms_exist,
    ensure_root_space,
    ensure_user_in_rooms,
    load_rooms,
    resolve_room_aliases,
)
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedThread,
    auto_resume_interrupted_threads,
    cleanup_stale_streaming_messages,
)
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, INTERNAL_USER_AGENT_NAME, create_agent_user
from mindroom.matrix_identifiers import extract_server_name_from_homeserver
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import mcp_tool_name
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import MemoryAutoFlushWorker, auto_flush_enabled
from mindroom.runtime_state import reset_runtime_state, set_runtime_failed, set_runtime_ready, set_runtime_starting
from mindroom.scheduling import set_scheduling_hook_registry
from mindroom.tool_approval import (
    initialize_approval_store,
    shutdown_approval_store,
    sync_unsynced_approval_event_resolutions,
)
from mindroom.tool_system.plugins import (
    PluginReloadResult,
    apply_prepared_plugin_reload,
    deactivate_plugins,
    get_configured_plugin_roots,
    load_plugins,
    prepare_plugin_reload,
    reload_plugins,
)
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot
from mindroom.workers.runtime import clear_worker_validation_snapshot_cache

from . import file_watcher
from .bot import AgentBot, TeamBot, create_bot_for_entity
from .config.main import Config, load_config
from .credentials_sync import sync_env_to_credentials
from .logging_config import get_logger, setup_logging
from .orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from .orchestration.plugin_watch import (
    capture_plugin_root_snapshots,
    replace_plugin_root_snapshots,
    sync_plugin_root_snapshots,
    watch_plugins_task,
)
from .orchestration.rooms import get_authorized_user_ids_to_invite, get_root_space_user_ids_to_invite
from .orchestration.runtime import (
    STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    STARTUP_RETRY_MAX_DELAY_SECONDS,
    EntityStartResults,
    cancel_logged_task,
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
from .runtime_support import (
    OwnedRuntimeSupport,
    build_owned_runtime_support,
    close_owned_runtime_support,
    sync_owned_runtime_support,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable
    from pathlib import Path
    from types import FrameType

    from mindroom.hooks import HookMatrixAdmin, HookMessageSender, HookRoomStatePutter, HookRoomStateQuerier

    from .constants import RuntimePaths
logger = get_logger(__name__)

_AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS = 1.0
_AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS = 30.0
_CONFIG_RELOAD_DEBOUNCE_SECONDS = 2.0

_TApprovalTransportResult = TypeVar("_TApprovalTransportResult")
_CONFIG_RELOAD_IDLE_POLL_SECONDS = 0.5
_CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS = 30.0
_CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS = 30.0
_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS = 120.0
_EMBEDDED_API_SHUTDOWN_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class _EmbeddedApiServerContext:
    """Shared identity fields for embedded API server lifecycle logs."""

    host: str
    port: int

    def log_context(self) -> dict[str, object]:
        """Return structured fields common to API server lifecycle logs."""
        return {"host": self.host, "port": self.port}


def _signal_name(sig: int) -> str:
    """Return a stable signal name for lifecycle logs."""
    for candidate in signal.Signals:
        if candidate.value == sig:
            return candidate.name
    return str(sig)


def _raise_embedded_api_server_exit(api_server: _EmbeddedApiServerContext, *, reason: str) -> NoReturn:
    """Raise the fatal lifecycle error for an unexpected API server exit."""
    logger.error(
        "fatal_embedded_api_server_exit",
        **api_server.log_context(),
        reason=reason,
    )
    msg = "Embedded API server exited unexpectedly"
    raise RuntimeError(msg)


@dataclass
class _ConfigReloadDrainState:
    """Track response-drain state for a queued config reload."""

    waiting_for_idle: bool = False
    wait_started_at: float | None = None
    last_warning_at: float | None = None
    request_started_at: float | None = None

    def reset(self) -> None:
        """Clear all drain tracking state."""
        self.waiting_for_idle = False
        self.wait_started_at = None
        self.last_warning_at = None
        self.request_started_at = None

    def begin_wait(self, *, now: float, requested_at: float) -> None:
        """Start a fresh drain window for the current reload request."""
        self.waiting_for_idle = True
        self.wait_started_at = now
        self.last_warning_at = None
        self.request_started_at = requested_at

    def should_reset_for_request(self, requested_at: float) -> bool:
        """Return whether a newer request should restart the drain window."""
        return self.waiting_for_idle and self.request_started_at != requested_at

    def wait_seconds(self, now: float) -> float:
        """Return how long the current drain window has been waiting."""
        if self.wait_started_at is None:
            return 0.0
        return now - self.wait_started_at

    def should_warn(
        self,
        *,
        now: float,
        warning_after_seconds: float,
        warning_interval_seconds: float,
    ) -> bool:
        """Return whether the current drain should emit a warning."""
        if self.wait_started_at is None or self.wait_seconds(now) < warning_after_seconds:
            return False
        if self.last_warning_at is None:
            return True
        return now - self.last_warning_at >= warning_interval_seconds

    def mark_warning(self, now: float) -> None:
        """Record the time a drain warning was logged."""
        self.last_warning_at = now

    def should_force_reload(self, *, now: float, force_after_seconds: float) -> bool:
        """Return whether the drain timeout has expired."""
        return self.wait_started_at is not None and self.wait_seconds(now) >= force_after_seconds


class _SignalAwareUvicornServer(uvicorn.Server):
    """Uvicorn server that marks the shared shutdown event on signal exit."""

    def __init__(self, config: uvicorn.Config, shutdown_requested: asyncio.Event | None) -> None:
        super().__init__(config)
        self._shutdown_requested = shutdown_requested

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Mirror Uvicorn signal handling and surface shutdown to the orchestrator."""
        del frame
        signal_number = int(sig)
        logger.info(
            "embedded_api_server_signal_received",
            signal_number=signal_number,
            signal_name=_signal_name(signal_number),
        )
        if self._shutdown_requested is not None:
            self._shutdown_requested.set()
        if self.should_exit and signal_number == int(signal.SIGINT):
            self.force_exit = True
            return
        self.should_exit = True


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    runtime_paths: RuntimePaths
    storage_path: Path = field(init=False)
    config_path: Path = field(init=False)
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _sync_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    _bot_start_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    _memory_auto_flush_worker: MemoryAutoFlushWorker | None = field(default=None, init=False)
    _memory_auto_flush_task: asyncio.Task | None = field(default=None, init=False)
    _config_reload_task: asyncio.Task | None = field(default=None, init=False)
    _config_reload_requested_at: float | None = field(default=None, init=False)
    _mcp_manager: MCPServerManager | None = field(default=None, init=False)
    _mcp_catalog_change_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _plugin_reload_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _runtime_support: OwnedRuntimeSupport = field(init=False)
    _event_cache_write_task_owner: object = field(default_factory=object, init=False)
    _plugin_watch_last_snapshot_by_root: dict[Path, dict[Path, int]] = field(default_factory=dict, init=False)
    _plugin_watch_state_revision: int = field(default=0, init=False)
    _knowledge_refresh_scheduler: KnowledgeRefreshScheduler = field(init=False)
    _knowledge_source_watcher: KnowledgeSourceWatcher = field(init=False)
    hook_registry: HookRegistry = field(default_factory=HookRegistry.empty, init=False)
    _runtime_shutdown_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _runtime_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Store canonical derived paths from the explicit runtime context."""
        self.storage_path = self.runtime_paths.storage_root
        self.config_path = self.runtime_paths.config_path
        self._runtime_support = build_owned_runtime_support(
            db_path=self.storage_path / "event_cache.db",
            logger=logger,
            background_task_owner=self._event_cache_write_task_owner,
        )
        self._knowledge_refresh_scheduler = KnowledgeRefreshScheduler()
        self._knowledge_source_watcher = KnowledgeSourceWatcher(self._knowledge_refresh_scheduler)

    @property
    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler:
        """Return the orchestrator-owned background knowledge refresh scheduler."""
        return self._knowledge_refresh_scheduler

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
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
        )
        self._memory_auto_flush_worker = worker
        self._memory_auto_flush_task = asyncio.create_task(worker.run(), name="memory_auto_flush_worker")

    def _reset_runtime_shutdown_event(self) -> asyncio.Event:
        """Create the shutdown event for the current orchestrator run."""
        shutdown_event = asyncio.Event()
        self._runtime_shutdown_event = shutdown_event
        return shutdown_event

    def _get_bot_by_agent_name(self, agent_name: str) -> AgentBot | TeamBot | None:
        """Return the managed bot that owns a given approval request."""
        return self.agent_bots.get(agent_name)

    def _capture_runtime_loop(self) -> None:
        """Remember the runtime loop that owns Matrix client I/O."""
        runtime_loop = asyncio.get_running_loop()
        if self._runtime_loop is None:
            self._runtime_loop = runtime_loop
            return
        if self._runtime_loop is not runtime_loop:
            msg = "MindRoom runtime loop is already bound to a different event loop."
            raise RuntimeError(msg)

    async def _run_on_runtime_loop(
        self,
        coroutine_factory: Callable[[], Coroutine[Any, Any, _TApprovalTransportResult]],
    ) -> _TApprovalTransportResult:
        """Run one coroutine on the orchestrator's runtime loop.

        Sync tool hooks can request approval through a private loop created by
        ``_run_coroutine_from_sync()``. The Matrix aiohttp client belongs to the
        runtime loop, so approval event sends and edits must hop back there.
        """
        runtime_loop = self._runtime_loop
        if runtime_loop is None or runtime_loop.is_closed():
            msg = "Approval runtime loop is not available."
            raise RuntimeError(msg)

        current_loop = asyncio.get_running_loop()
        if current_loop is runtime_loop:
            return await coroutine_factory()

        future = asyncio.run_coroutine_threadsafe(coroutine_factory(), runtime_loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    @staticmethod
    def _approval_thread_relation(thread_id: str) -> dict[str, object]:
        """Return a threaded relation payload for approval events."""
        return {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": thread_id},
        }

    async def _send_approval_event(
        self,
        room_id: str,
        thread_id: str | None,
        agent_name: str,
        content: dict[str, Any],
    ) -> str | None:
        """Send one custom approval event into the active Matrix thread."""
        return await self._run_on_runtime_loop(
            lambda: self._send_approval_event_now(room_id, thread_id, agent_name, content),
        )

    async def _send_approval_event_now(
        self,
        room_id: str,
        thread_id: str | None,
        agent_name: str,
        content: dict[str, Any],
    ) -> str | None:
        """Send one custom approval event on the current loop."""
        bot = self._get_bot_by_agent_name(agent_name)
        if bot is None or bot.client is None:
            return None
        send_content = dict(content)
        if thread_id is not None:
            send_content["m.relates_to"] = self._approval_thread_relation(thread_id)
        response = await bot.client.room_send(
            room_id=room_id,
            message_type="io.mindroom.tool_approval",
            content=send_content,
        )
        if isinstance(response, nio.RoomSendResponse):
            return str(response.event_id)
        logger.warning(
            "Failed to send approval Matrix event",
            room_id=room_id,
            thread_id=thread_id,
            agent_name=agent_name,
            response=str(response),
        )
        return None

    async def _edit_approval_event(
        self,
        room_id: str,
        event_id: str,
        agent_name: str,
        new_content: dict[str, Any],
    ) -> None:
        """Edit one previously sent approval event."""
        await self._run_on_runtime_loop(
            lambda: self._edit_approval_event_now(room_id, event_id, agent_name, new_content),
        )

    async def _edit_approval_event_now(
        self,
        room_id: str,
        event_id: str,
        agent_name: str,
        new_content: dict[str, Any],
    ) -> None:
        """Edit one previously sent approval event on the current loop."""
        bot = self._get_bot_by_agent_name(agent_name)
        if bot is None:
            return
        if bot.client is None:
            msg = f"Approval Matrix client is unavailable for agent '{agent_name}'"
            raise RuntimeError(msg)

        thread_id = new_content.get("thread_id")
        if thread_id is not None and not isinstance(thread_id, str):
            msg = "Approval thread_id must be a string when present."
            raise TypeError(msg)

        replacement_content = {key: value for key, value in new_content.items() if key != "thread_id"}
        if isinstance(thread_id, str) and thread_id:
            replacement_content["m.relates_to"] = self._approval_thread_relation(thread_id)
        response = await bot.client.room_send(
            room_id=room_id,
            message_type="io.mindroom.tool_approval",
            content={
                **replacement_content,
                "m.new_content": replacement_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            },
        )
        if not isinstance(response, nio.RoomSendResponse):
            logger.warning(
                "Failed to edit approval Matrix event",
                room_id=room_id,
                event_id=event_id,
                agent_name=agent_name,
                response=str(response),
            )

    def _bind_runtime_support_services(self, bot: AgentBot | TeamBot) -> None:
        """Bind the current runtime support services to one managed bot."""
        bot.event_cache = self._runtime_support.event_cache
        bot.event_cache_write_coordinator = self._runtime_support.event_cache_write_coordinator
        bot.startup_thread_prewarm_registry = self._runtime_support.startup_thread_prewarm_registry

    def _rebind_runtime_support_services(self) -> None:
        """Rebind the current runtime support services to every managed bot."""
        for bot in self.agent_bots.values():
            self._bind_runtime_support_services(bot)

    async def _sync_event_cache_service(self, config: Config) -> None:
        """Ensure the runtime has one initialized shared event-cache service."""
        self._runtime_support = await sync_owned_runtime_support(
            self._runtime_support,
            cache_config=config.cache,
            runtime_paths=self.runtime_paths,
            logger=logger,
            background_task_owner=self._event_cache_write_task_owner,
            init_failure_reason_prefix="shared_runtime_init_failed",
            log_db_path_change=True,
        )
        self._rebind_runtime_support_services()

    async def _close_runtime_support_services(self) -> None:
        """Close the shared runtime-owned cache services."""
        await close_owned_runtime_support(self._runtime_support, logger=logger)

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
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            INTERNAL_USER_AGENT_NAME,
            config.mindroom_user.display_name,
            username=config.mindroom_user.username,
            runtime_paths=self.runtime_paths,
        )
        logger.info("user_account_ready", user_id=user_account.user_id)

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

    async def _cancel_config_reload_task(self) -> None:
        """Cancel any queued config reload task."""
        task = self._config_reload_task
        self._config_reload_task = None
        self._config_reload_requested_at = None
        await cancel_logged_task(task)

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

                config = self.config
                if config is not None and entity_name in await self._retry_blocked_mcp_entities({entity_name}, config):
                    start_status = False
                else:
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

    def in_flight_response_count(self) -> int:
        """Return the number of active response tasks across all managed bots."""
        return sum(bot.in_flight_response_count for bot in self.agent_bots.values())

    def request_config_reload(self) -> None:
        """Queue a debounced config reload for the running orchestrator."""
        if not self.running:
            logger.info("Ignoring config change while startup is still in progress")
            return
        self._config_reload_requested_at = asyncio.get_running_loop().time()
        if self._config_reload_task is not None and not self._config_reload_task.done():
            logger.info("Configuration reload already queued; extending debounce window")
            return
        logger.info("Queued configuration reload")
        self._config_reload_task = create_logged_task(
            self._run_config_reload_loop(),
            name="config_reload",
            failure_message="Queued config reload failed",
        )

    async def _wait_for_reload_debounce(
        self,
        requested_at: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Sleep until the debounce window closes for a queued reload request."""
        reload_at = requested_at + _CONFIG_RELOAD_DEBOUNCE_SECONDS
        delay_seconds = reload_at - loop.time()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    async def _should_defer_reload_for_active_responses(
        self,
        *,
        drain_state: _ConfigReloadDrainState,
        requested_at: float,
        active_response_count: int,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Return whether a queued reload should keep waiting for responses to finish."""
        if active_response_count <= 0:
            return False

        now = loop.time()
        if not drain_state.waiting_for_idle:
            logger.info(
                "Deferring configuration reload until active responses finish",
                active_response_count=active_response_count,
            )
            drain_state.begin_wait(now=now, requested_at=requested_at)
        elif drain_state.should_warn(
            now=now,
            warning_after_seconds=_CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS,
            warning_interval_seconds=_CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS,
        ):
            logger.warning(
                "Configuration reload still waiting for active responses to finish",
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
            )
            drain_state.mark_warning(now)

        if drain_state.should_force_reload(
            now=now,
            force_after_seconds=_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS,
        ):
            logger.error(
                "Forcing configuration reload while responses are still active",
                active_response_count=active_response_count,
                drain_wait_seconds=round(drain_state.wait_seconds(now), 1),
                timeout_seconds=_CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS,
            )
            return False

        await asyncio.sleep(_CONFIG_RELOAD_IDLE_POLL_SECONDS)
        return True

    async def _apply_queued_config_reload(self) -> None:
        """Apply one queued config reload attempt and log the result."""
        self._config_reload_requested_at = None
        logger.info("Configuration file changed, checking for updates...")
        try:
            updated = await self.update_config()
        except Exception:
            logger.exception("Configuration update failed; will retry if a new change is queued")
            return
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")

    async def _run_config_reload_loop(self) -> None:
        """Apply queued config reloads after debounce and response drain."""
        current_task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        drain_state = _ConfigReloadDrainState()

        try:
            while self.running and self._config_reload_requested_at is not None:
                requested_at = self._config_reload_requested_at
                await self._wait_for_reload_debounce(requested_at, loop)
                if self._config_reload_requested_at != requested_at:
                    # A newer config change superseded the current one.
                    # Reset drain state so the new change gets a full drain window.
                    drain_state.reset()
                    continue

                if drain_state.should_reset_for_request(requested_at):
                    # A newer config change arrived while we were already waiting
                    # for responses to drain, so restart the drain window.
                    drain_state.reset()
                    continue

                active_response_count = self.in_flight_response_count()
                if await self._should_defer_reload_for_active_responses(
                    drain_state=drain_state,
                    requested_at=requested_at,
                    active_response_count=active_response_count,
                    loop=loop,
                ):
                    continue

                if drain_state.waiting_for_idle and active_response_count == 0:
                    logger.info("Active responses finished; applying queued configuration reload")
                if drain_state.waiting_for_idle:
                    drain_state.reset()

                await self._apply_queued_config_reload()
        finally:
            if self._config_reload_task is current_task:
                self._config_reload_task = None

    async def _sync_runtime_support_services(
        self,
        config: Config,
        *,
        start_watcher: bool,
    ) -> None:
        """Refresh runtime support services that depend on the active config."""
        await self._knowledge_source_watcher.sync(
            config=config if start_watcher else None,
            runtime_paths=self.runtime_paths,
        )
        ensure_default_agent_workspaces(config, self.storage_path)
        await self._sync_event_cache_service(config)
        await self._sync_memory_auto_flush_worker()

    async def _stop_mcp_manager(self) -> None:
        """Stop the MCP manager and clear the active runtime binding."""
        manager = self._mcp_manager
        self._mcp_manager = None
        bind_mcp_server_manager(None)
        if manager is not None:
            await manager.shutdown()

    async def _sync_mcp_manager(self, config: Config) -> set[str]:
        """Create or refresh the orchestrator-owned MCP manager."""
        manager = self._mcp_manager
        if manager is None:
            manager = MCPServerManager(
                self.runtime_paths,
                on_catalog_change=self._handle_mcp_catalog_change,
            )
            self._mcp_manager = manager
        bind_mcp_server_manager(manager)
        return await manager.sync_servers(config)

    def _entities_blocked_by_failed_mcp_servers(self, entity_names: set[str], config: Config) -> set[str]:
        """Return entities whose required MCP servers are currently unavailable."""
        manager = self._mcp_manager
        if manager is None:
            return set()
        failed_server_ids = manager.failed_server_ids()
        if not failed_server_ids:
            return set()
        blocked_entities = config.get_entities_referencing_tools(
            {mcp_tool_name(server_id) for server_id in failed_server_ids},
        )
        return blocked_entities & entity_names

    async def _retry_blocked_mcp_entities(self, entity_names: set[str], config: Config) -> set[str]:
        """Retry failed MCP discovery once before deferring dependent entity startup."""
        blocked_entities = self._entities_blocked_by_failed_mcp_servers(entity_names, config)
        if not blocked_entities:
            return set()
        await self._sync_mcp_manager(config)
        return self._entities_blocked_by_failed_mcp_servers(entity_names, config)

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
                self.runtime_paths,
                self.storage_path,
                config_path=self.config_path,
            ),
        )
        bot.orchestrator = self
        bot.hook_registry = self.hook_registry
        self._bind_runtime_support_services(bot)
        self.agent_bots[entity_name] = bot
        return bot

    def _build_hook_registry(self, config: Config) -> HookRegistry:
        """Load plugins and rebuild the immutable hook-registry snapshot."""
        plugins = load_plugins(config, self.runtime_paths)
        return HookRegistry.from_plugins(plugins)

    def _activate_hook_registry(self, hook_registry: HookRegistry) -> None:
        """Commit one hook-registry snapshot to the live runtime."""
        set_scheduling_hook_registry(hook_registry)
        self.hook_registry = hook_registry
        for bot in self.agent_bots.values():
            bot.hook_registry = hook_registry

    def _sync_plugin_watch_roots(self, config: Config | None = None) -> tuple[Path, ...]:
        """Align watcher baselines with the currently configured plugin roots."""
        active_config = self.config if config is None else config
        configured_roots = (
            get_configured_plugin_roots(active_config, self.runtime_paths) if active_config is not None else ()
        )
        sync_plugin_root_snapshots(configured_roots, self._plugin_watch_last_snapshot_by_root)
        return configured_roots

    def _replace_plugin_watch_snapshots(
        self,
        configured_roots: tuple[Path, ...],
        root_snapshots: dict[Path, dict[Path, int]],
    ) -> None:
        """Replace watcher baselines and clear any stale pending dirty state."""
        replace_plugin_root_snapshots(
            configured_roots,
            root_snapshots,
            self._plugin_watch_last_snapshot_by_root,
        )
        self._plugin_watch_state_revision += 1

    def _refresh_plugin_watch_state(self, config: Config | None = None) -> tuple[Path, ...]:
        """Capture fresh watcher baselines for the current plugin roots."""
        active_config = self.config if config is None else config
        configured_roots = (
            get_configured_plugin_roots(active_config, self.runtime_paths) if active_config is not None else ()
        )
        self._replace_plugin_watch_snapshots(
            configured_roots,
            capture_plugin_root_snapshots(configured_roots),
        )
        return configured_roots

    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
    ) -> PluginReloadResult:
        """Rebuild and atomically swap the live plugin registry snapshot."""
        if not self.running:
            msg = "Plugin reload unavailable until startup finishes."
            raise RuntimeError(msg)
        async with self._plugin_reload_lock:
            config = self._require_config()
            logger.info(
                "Reloading plugins",
                source=source,
                changed_paths=[str(path) for path in changed_paths],
            )
            try:
                result = reload_plugins(config, self.runtime_paths)
            except Exception:
                recovery_result, warning_message, warning_kwargs = _recover_failed_plugin_reload(
                    config,
                    self.runtime_paths,
                )
                self._activate_hook_registry(recovery_result.hook_registry)
                clear_worker_validation_snapshot_cache()
                self._refresh_plugin_watch_state(config)
                logger.warning(warning_message, source=source, **warning_kwargs)
                raise
            self._activate_hook_registry(result.hook_registry)
            clear_worker_validation_snapshot_cache()
            self._refresh_plugin_watch_state(config)
            logger.info(
                "Plugin reload complete",
                source=source,
                active_plugins=list(result.active_plugin_names),
                cancelled_task_count=result.cancelled_task_count,
            )
            return result

    async def _apply_plugin_changes_for_config_update(
        self,
        *,
        current_config: Config,
        new_config: Config,
        changed_server_ids: set[str],
    ) -> set[str]:
        """Stage and commit plugin changes without interleaving live reloads."""
        async with self._plugin_reload_lock:
            prepared_plugin_roots = get_configured_plugin_roots(new_config, self.runtime_paths)
            prepared_plugin_root_snapshots = capture_plugin_root_snapshots(prepared_plugin_roots)
            prepared_plugin_reload = prepare_plugin_reload(
                new_config,
                self.runtime_paths,
                skip_broken_plugins=True,
            )
            pre_stopped_mcp_entities = await self._stop_entities_before_mcp_sync(
                current_config,
                new_config,
                changed_server_ids,
            )
            self.config = new_config
            new_hook_registry = apply_prepared_plugin_reload(
                prepared_plugin_reload,
                cancel_existing_tasks=True,
            ).hook_registry
            self._replace_plugin_watch_snapshots(
                prepared_plugin_roots,
                prepared_plugin_root_snapshots,
            )
            self._activate_hook_registry(new_hook_registry)
            clear_worker_validation_snapshot_cache()
            return pre_stopped_mcp_entities

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

        config = self.config
        blocked_entities = (
            await self._retry_blocked_mcp_entities({entity_name for entity_name, _ in entity_bots}, config)
            if config is not None
            else set()
        )
        if blocked_entities:
            results.retryable_entities.extend(sorted(blocked_entities))
            entity_bots = [
                (entity_name, bot) for entity_name, bot in entity_bots if entity_name not in blocked_entities
            ]
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
        self._capture_runtime_loop()
        set_runtime_starting("Loading config and preparing agents")
        logger.info("Initializing multi-agent system...")

        initialize_approval_store(
            self.runtime_paths,
            sender=self._send_approval_event,
            editor=self._edit_approval_event,
        )
        config = load_config(self.runtime_paths, tolerate_plugin_load_errors=True)
        hook_registry = self._build_hook_registry(config)
        await self._prepare_user_account(config, update_runtime_state=True)
        self.config = config
        self._activate_hook_registry(hook_registry)
        await self._sync_mcp_manager(config)
        await self._sync_event_cache_service(config)
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
        config = self._require_config()
        if ROUTER_AGENT_NAME in self._entities_blocked_by_failed_mcp_servers({ROUTER_AGENT_NAME}, config):
            msg = "Router bot depends on unavailable MCP servers"
            raise RuntimeError(msg)
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

    def hook_message_sender(self) -> HookMessageSender | None:
        """Return a router-backed sender for hook contexts when available."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None:
            return None
        return router_bot._hook_send_message

    def hook_room_state_querier(self) -> HookRoomStateQuerier | None:
        """Return a router-backed room-state querier for hook contexts when available."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None:
            return None
        return build_hook_room_state_querier(router_bot.client)

    def hook_room_state_putter(self) -> HookRoomStatePutter | None:
        """Return a router-backed room-state putter for hook contexts when available."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None:
            return None
        return build_hook_room_state_putter(router_bot.client)

    def hook_matrix_admin(self) -> HookMatrixAdmin | None:
        """Return a router-backed Matrix admin helper for hook contexts when available."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None:
            return None
        return build_hook_matrix_admin(router_bot.client, self.runtime_paths)

    def _log_degraded_startup(self, failed_agents: list[str]) -> None:
        """Log degraded startup status for failed non-router bots."""
        if failed_agents:
            logger.warning(
                "System starting in degraded mode",
                failed_agents=failed_agents,
                failed_agent_count=len(failed_agents),
                operational_agent_count=len(self.agent_bots) - len(failed_agents),
                total_agent_count=len(self.agent_bots),
            )
            return
        logger.info("All agent bots started successfully")

    async def _cleanup_stale_streams_after_restart(
        self,
        bots: list[AgentBot | TeamBot],
        config: Config,
    ) -> list[InterruptedThread]:
        """Cleanup stale streams for started bots before sync loops begin."""
        bot_user_ids = {bot.agent_user.user_id for bot in bots if bot.client is not None and bot.agent_user.user_id}
        if not bot_user_ids:
            return []

        cleaned_count = 0
        interrupted_threads: list[InterruptedThread] = []
        for bot in bots:
            if bot.client is None or not bot.agent_user.user_id:
                continue
            try:
                bot_cleaned_count, bot_interrupted_threads = await cleanup_stale_streaming_messages(
                    bot.client,
                    bot_user_id=bot.agent_user.user_id,
                    bot_user_ids=bot_user_ids,
                    config=config,
                    runtime_paths=self.runtime_paths,
                    conversation_cache=bot._conversation_cache,
                )
                cleaned_count += bot_cleaned_count
                interrupted_threads.extend(bot_interrupted_threads)
            except Exception as exc:
                logger.warning(
                    "Could not cleanup stale streaming messages (non-critical)",
                    agent_name=bot.agent_name,
                    error=str(exc),
                )

        if cleaned_count > 0:
            logger.info("Cleaned stale streaming messages", count=cleaned_count)
        return interrupted_threads

    async def _auto_resume_after_restart(
        self,
        interrupted_threads: list[InterruptedThread],
        config: Config,
    ) -> None:
        """Queue visible Matrix resume relays from the router."""
        if not config.defaults.auto_resume_after_restart or not interrupted_threads:
            return
        router_bot = self._router_bot()
        if router_bot is None or router_bot.client is None:
            logger.warning("Auto-resume after restart skipped because the router client is unavailable")
            return

        try:
            resumed_count = await auto_resume_interrupted_threads(
                router_bot.client,
                interrupted_threads,
                config=config,
                runtime_paths=self.runtime_paths,
                conversation_cache=router_bot._conversation_cache,
            )
            if resumed_count > 0:
                logger.info("Queued auto-resume messages after restart", count=resumed_count)
        except Exception as exc:
            logger.warning("Could not auto-resume interrupted threads (non-critical)", error=str(exc))

    async def _start_runtime(self) -> None:
        """Run the startup sequence before handing off to the sync loops."""
        runtime_shutdown_event = self._reset_runtime_shutdown_event()
        await wait_for_matrix_homeserver(runtime_paths=self.runtime_paths)
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
        try:
            await sync_unsynced_approval_event_resolutions()
        except Exception as exc:
            logger.warning("tool_approval_resolution_replay_failed", error=str(exc))
        interrupted_threads = await self._cleanup_stale_streams_after_restart(started_bots, config)
        await self._auto_resume_after_restart(interrupted_threads, config)

        self.running = True

        set_runtime_starting("Starting background workers")
        await self._sync_runtime_support_services(config, start_watcher=True)

        # Create sync tasks for each bot with automatic restart on failure.
        set_runtime_starting("Starting Matrix sync loops")
        for entity_name, bot in self.agent_bots.items():
            if bot.running:
                self._start_sync_task(entity_name, bot)

        for entity_name in start_results.retryable_entities:
            await self._schedule_bot_start_retry(entity_name)

        set_runtime_ready()
        # Stay alive until explicit shutdown. Hot reload replaces sync tasks in
        # self._sync_tasks, so awaiting the initial task generation would let a
        # config-triggered restart look like normal orchestrator completion.
        await runtime_shutdown_event.wait()

    async def _load_initial_config(self, new_config: Config, hook_registry: HookRegistry) -> bool:
        """Handle config loading before the runtime has an active config."""
        await self._prepare_user_account(new_config, update_runtime_state=not self.running)
        self.config = new_config
        self._activate_hook_registry(hook_registry)
        await self._sync_mcp_manager(new_config)
        await self._sync_runtime_support_services(new_config, start_watcher=self.running)
        clear_worker_validation_snapshot_cache()
        return False

    async def _update_unchanged_bots(self, plan: ConfigUpdatePlan) -> None:
        """Apply the new config to bots that do not require restart."""
        for entity_name, bot in self.agent_bots.items():
            if entity_name in plan.entities_to_restart:
                continue
            bot.config = plan.new_config
            bot.enable_streaming = plan.new_config.defaults.enable_streaming
            bot.hook_registry = self.hook_registry
            await bot._set_presence_with_model_info()
            logger.debug("bot_config_updated", agent=entity_name)

    @staticmethod
    def _plugin_change_paths(current_config: Config, new_config: Config) -> tuple[str, ...]:
        """Return plugin paths whose entry config changed across a reload."""
        old_entries = {entry.path: entry.model_dump(mode="python") for entry in current_config.plugins}
        new_entries = {entry.path: entry.model_dump(mode="python") for entry in new_config.plugins}
        changed_paths = {
            path for path in set(old_entries) | set(new_entries) if old_entries.get(path) != new_entries.get(path)
        }
        return tuple(sorted(changed_paths))

    async def _emit_config_reloaded(
        self,
        *,
        new_config: Config,
        changed_entities: set[str],
        added_entities: set[str],
        removed_entities: set[str],
        plugin_changes: tuple[str, ...],
    ) -> None:
        """Emit the config:reloaded observer event after applying a new snapshot."""
        if not self.hook_registry.has_hooks(EVENT_CONFIG_RELOADED):
            return

        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        context = ConfigReloadedContext(
            event_name=EVENT_CONFIG_RELOADED,
            plugin_name="",
            settings={},
            config=new_config,
            runtime_paths=self.runtime_paths,
            logger=logger.bind(event_name=EVENT_CONFIG_RELOADED),
            correlation_id=f"config-reload:{uuid4().hex}",
            runtime_started_at=(router_bot.runtime_started_at if isinstance(router_bot, AgentBot) else time.time()),
            message_sender=self.hook_message_sender(),
            agent_message_snapshot_reader=(
                router_bot._hook_agent_message_snapshot if isinstance(router_bot, AgentBot) else None
            ),
            matrix_admin=self.hook_matrix_admin(),
            room_state_querier=self.hook_room_state_querier(),
            room_state_putter=self.hook_room_state_putter(),
            changed_entities=tuple(sorted(changed_entities)),
            added_entities=tuple(sorted(added_entities)),
            removed_entities=tuple(sorted(removed_entities)),
            plugin_changes=plugin_changes,
        )
        await emit(self.hook_registry, EVENT_CONFIG_RELOADED, context)

    async def _remove_deleted_entities(self, removed_entities: set[str]) -> None:
        """Cancel, clean up, and unregister entities removed from config."""
        for entity_name in removed_entities:
            await self._cancel_bot_start_task(entity_name)
            await cancel_sync_task(entity_name, self._sync_tasks)

            bot = self.agent_bots.pop(entity_name, None)
            if bot is not None:
                await bot.cleanup()

    async def _stop_entities_before_mcp_sync(
        self,
        current_config: Config,
        new_config: Config,
        changed_server_ids: set[str],
    ) -> set[str]:
        """Stop MCP-dependent entities before removing or reconfiguring their servers."""
        if not changed_server_ids:
            return set()

        affected_entities = current_config.get_entities_referencing_tools(
            {mcp_tool_name(server_id) for server_id in changed_server_ids},
        ) | new_config.get_entities_referencing_tools({mcp_tool_name(server_id) for server_id in changed_server_ids})
        if not affected_entities:
            return set()

        for entity_name in affected_entities:
            await self._cancel_bot_start_task(entity_name)
        await stop_entities(affected_entities, self.agent_bots, self._sync_tasks)
        return affected_entities

    async def _restart_changed_entities(
        self,
        plan: ConfigUpdatePlan,
        *,
        already_stopped_entities: set[str] | None = None,
    ) -> tuple[set[str], list[str], list[str]]:
        """Restart or create entities affected by the config change."""
        entities_to_stop = plan.entities_to_restart - (already_stopped_entities or set())
        if entities_to_stop:
            for entity_name in entities_to_stop:
                await self._cancel_bot_start_task(entity_name)
            await stop_entities(entities_to_stop, self.agent_bots, self._sync_tasks)

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

    async def _handle_mcp_catalog_change(self, server_id: str) -> None:
        """Restart entities that reference one changed MCP catalog."""
        async with self._mcp_catalog_change_lock:
            if not self.running or self.config is None:
                return
            clear_worker_validation_snapshot_cache()
            changed_entities = self.config.get_entities_referencing_tools({mcp_tool_name(server_id)})
            if not changed_entities:
                return
            logger.info(
                "Restarting entities after MCP catalog change",
                server_id=server_id,
                entities=sorted(changed_entities),
            )
            for entity_name in changed_entities:
                await self._cancel_bot_start_task(entity_name)
            await stop_entities(changed_entities, self.agent_bots, self._sync_tasks)
            start_results = await self._create_and_start_entities(
                changed_entities,
                self.config,
                start_sync_tasks=True,
            )
            for entity_name in start_results.retryable_entities:
                await self._schedule_bot_start_retry(entity_name)
            if start_results.permanently_failed_entities:
                logger.warning(
                    "MCP catalog restart left some bots disabled",
                    server_id=server_id,
                    entities=start_results.permanently_failed_entities,
                )

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
        new_config = load_config(self.runtime_paths, tolerate_plugin_load_errors=True)

        if not self.config:
            return await self._load_initial_config(new_config, self._build_hook_registry(new_config))

        current_config = self._require_config()
        plugin_changes = self._plugin_change_paths(current_config, new_config)
        plan = build_config_update_plan(
            current_config=current_config,
            new_config=new_config,
            configured_entities=set(self._configured_entity_names(new_config)),
            existing_entities=set(self.agent_bots.keys()),
            agent_bots=self.agent_bots,
        )
        if plugin_changes:
            plan = replace(plan, entities_to_restart=plan.entities_to_restart | set(self.agent_bots))

        if plan.mindroom_user_changed:
            await self._prepare_user_account(new_config, update_runtime_state=not self.running)

        if plugin_changes:
            pre_stopped_mcp_entities = await self._apply_plugin_changes_for_config_update(
                current_config=current_config,
                new_config=new_config,
                changed_server_ids=plan.changed_mcp_servers,
            )
        else:
            pre_stopped_mcp_entities = await self._stop_entities_before_mcp_sync(
                current_config,
                new_config,
                plan.changed_mcp_servers,
            )
            # Only apply the new config after validation and account checks succeed.
            self.config = new_config
            self._sync_plugin_watch_roots(new_config)
            self._activate_hook_registry(self.hook_registry)
            clear_worker_validation_snapshot_cache()
        changed_runtime_mcp_servers = await self._sync_mcp_manager(new_config)
        await self._sync_event_cache_service(new_config)
        logger.info(
            "updating_config_authorization",
            authorized_user_ids=new_config.authorization.global_users,
        )
        if changed_runtime_mcp_servers:
            plan = replace(
                plan,
                entities_to_restart=plan.entities_to_restart
                | new_config.get_entities_referencing_tools(
                    {mcp_tool_name(server_id) for server_id in changed_runtime_mcp_servers},
                ),
            )
        await self._update_unchanged_bots(plan)

        if plan.only_support_service_changes:
            await self._sync_runtime_support_services(new_config, start_watcher=self.running)
            await self._emit_config_reloaded(
                new_config=new_config,
                changed_entities=set(),
                added_entities=plan.new_entities,
                removed_entities=plan.removed_entities,
                plugin_changes=plugin_changes,
            )
            return False

        changed_entities, retryable_entities, permanently_failed_entities = await self._restart_changed_entities(
            plan,
            already_stopped_entities=pre_stopped_mcp_entities,
        )
        await self._reconcile_post_update_rooms(plan, changed_entities)

        for entity_name in retryable_entities:
            await self._schedule_bot_start_retry(entity_name)

        if permanently_failed_entities:
            logger.warning(
                "Configuration update left some bots disabled due to permanent startup errors",
                agent_names=permanently_failed_entities,
            )

        await self._sync_runtime_support_services(new_config, start_watcher=self.running)
        await self._emit_config_reloaded(
            new_config=new_config,
            changed_entities=changed_entities,
            added_entities=plan.new_entities,
            removed_entities=plan.removed_entities,
            plugin_changes=plugin_changes,
        )

        logger.info(
            "configuration_update_complete",
            affected_bot_count=len(plan.entities_to_restart) + len(plan.new_entities),
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
            bot.rooms = resolve_room_aliases(room_aliases, runtime_paths=self.runtime_paths)

        async def _ensure_internal_user_memberships() -> None:
            all_rooms = load_rooms(runtime_paths=self.runtime_paths)
            all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
            if all_room_ids and config.mindroom_user is not None:
                await ensure_user_in_rooms(
                    constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
                    all_room_ids,
                    self.runtime_paths,
                )

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
        room_ids = await ensure_all_rooms_exist(router_bot.client, config, self.runtime_paths)
        logger.info("ensured_room_existence", room_count=len(room_ids))
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
        root_space_id = await ensure_root_space(
            router_bot.client,
            config,
            self.runtime_paths,
            normalized_room_ids,
        )
        if root_space_id is None:
            return

        invite_user_ids = get_root_space_user_ids_to_invite(config, self.runtime_paths)
        if not invite_user_ids:
            return

        current_members = await get_room_members(router_bot.client, root_space_id)
        for user_id in sorted(invite_user_ids):
            if user_id in current_members:
                continue
            success = await invite_to_room(router_bot.client, root_space_id, user_id)
            if success:
                logger.info("invited_user_to_root_space", user_id=user_id, room_id=root_space_id)
            else:
                logger.warning("invite_user_to_root_space_failed", user_id=user_id, room_id=root_space_id)

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

        state = matrix_state_for_runtime(self.runtime_paths)
        user_account = state.get_account(INTERNAL_USER_ACCOUNT_KEY)
        if config.mindroom_user is None or not user_account:
            return authorized_user_ids

        server_name = extract_server_name_from_homeserver(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            runtime_paths=self.runtime_paths,
        )
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
            if not is_authorized_sender(authorized_user_id, config, room_id, self.runtime_paths):
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

        server_name = extract_server_name_from_homeserver(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            runtime_paths=self.runtime_paths,
        )
        authorized_user_ids = get_authorized_user_ids_to_invite(config)
        authorized_user_ids = await self._invite_internal_user_to_rooms(
            config,
            joined_rooms,
            authorized_user_ids,
        )

        for room_id in joined_rooms:
            configured_bots = configured_bot_usernames_for_room(config, room_id, self.runtime_paths)
            if not configured_bots:
                continue

            current_members = await get_room_members(router_bot.client, room_id)
            await self._invite_authorized_users_to_room(room_id, current_members, authorized_user_ids, config)
            await self._invite_configured_bots_to_room(room_id, current_members, configured_bots, server_name)

        logger.info("Ensured room invitations for all configured agents and authorized users")

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        if self._runtime_shutdown_event is not None:
            self._runtime_shutdown_event.set()
        await self._cancel_config_reload_task()
        await self._stop_memory_auto_flush_worker()
        await self._knowledge_source_watcher.shutdown()
        await self._knowledge_refresh_scheduler.shutdown()
        await self._cancel_bot_start_tasks()
        await self._stop_mcp_manager()

        # Cancel sync tasks first so shutdown does not race with active sync loops.
        for entity_name in list(self._sync_tasks.keys()):
            await cancel_sync_task(entity_name, self._sync_tasks)

        for bot in self.agent_bots.values():
            bot.running = False

        await shutdown_approval_store()
        stop_tasks = [bot.stop(reason="shutdown") for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        await self._close_runtime_support_services()
        logger.info("All agent bots stopped")


def _recover_failed_plugin_reload(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[PluginReloadResult, str, dict[str, object]]:
    """Return the fail-broken recovery result after one strict reload failure."""
    try:
        recovery_result = reload_plugins(config, runtime_paths, skip_broken_plugins=True)
    except Exception as degraded_error:
        return (
            deactivate_plugins(),
            "Plugin reload failed; all plugins deactivated",
            {"degraded_error": str(degraded_error)},
        )
    return (
        recovery_result,
        "Plugin reload failed; active plugin set degraded",
        {"active_plugins": list(recovery_result.active_plugin_names)},
    )


async def _handle_config_change(orchestrator: MultiAgentOrchestrator) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed; queueing hot reload")
    orchestrator.request_config_reload()


async def _watch_config_task(config_path: Path, orchestrator: MultiAgentOrchestrator) -> None:
    """Watch config file for changes."""

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator)

    await file_watcher.watch_file(config_path, on_config_change)


async def _watch_skills_task(orchestrator: MultiAgentOrchestrator) -> None:
    """Watch skill roots for changes and clear cached skills."""
    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    last_snapshot = await asyncio.to_thread(get_skill_snapshot)
    while orchestrator.running:
        await asyncio.sleep(1.0)
        snapshot = await asyncio.to_thread(get_skill_snapshot)
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            clear_skill_cache()
            logger.info("Skills changed; cache cleared")


async def _run_api_server(
    host: str,
    port: int,
    log_level: str,
    runtime_paths: RuntimePaths,
    knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    shutdown_requested: asyncio.Event | None = None,
) -> None:
    """Run the bundled dashboard/API server as an asyncio task."""
    from mindroom.api import main as api_main  # noqa: PLC0415

    api_server = _EmbeddedApiServerContext(host=host, port=port)
    api_main.initialize_api_app(api_main.app, runtime_paths)
    if knowledge_refresh_scheduler is not None:
        api_main.bind_orchestrator_knowledge_refresh_scheduler(api_main.app, knowledge_refresh_scheduler)
    config = uvicorn.Config(api_main.app, host=host, port=port, log_level=log_level.lower())
    server = _SignalAwareUvicornServer(config, shutdown_requested)
    logger.info("embedded_api_server_started", **api_server.log_context())
    await server.serve()
    shutdown_expected = shutdown_requested.is_set() if shutdown_requested is not None else False
    logger.info(
        "embedded_api_server_serve_returned",
        **api_server.log_context(),
        shutdown_expected=shutdown_expected,
        server_should_exit=server.should_exit,
        server_force_exit=server.force_exit,
    )
    if not shutdown_expected:
        _raise_embedded_api_server_exit(
            api_server,
            reason="server.serve() returned while application shutdown was not requested",
        )


async def _run_auxiliary_task_forever(
    task_name: str,
    operation: Callable[[], Awaitable[None]],
    *,
    should_restart: Callable[[], bool] | None = None,
) -> None:
    """Restart a non-critical background task whenever it exits or crashes."""
    restart_allowed = (lambda: True) if should_restart is None else should_restart
    restart_count = 0
    while restart_allowed():
        started_at = time.monotonic()
        try:
            await operation()
            if not restart_allowed():
                return
            logger.warning("Auxiliary task exited; restarting", task_name=task_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not restart_allowed():
                return
            logger.exception(
                "Auxiliary task crashed; restarting",
                task_name=task_name,
            )
        if not restart_allowed():
            return
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


async def _wait_for_runtime_completion(
    *,
    orchestrator_task: asyncio.Task[None],
    shutdown_wait_task: asyncio.Task[bool],
    api_task: asyncio.Task[None] | None,
    shutdown_requested: asyncio.Event,
    api_server: _EmbeddedApiServerContext,
) -> None:
    """Wait until the orchestrator, API server, or shutdown signal ends the run."""
    monitored_tasks: set[asyncio.Task] = {orchestrator_task, shutdown_wait_task}
    if api_task is not None:
        monitored_tasks.add(api_task)
    done, _pending = await asyncio.wait(monitored_tasks, return_when=asyncio.FIRST_COMPLETED)
    await _consume_completed_runtime_tasks(
        done,
        orchestrator_task=orchestrator_task,
        shutdown_wait_task=shutdown_wait_task,
        api_task=api_task,
        shutdown_requested=shutdown_requested,
        api_server=api_server,
    )

    if shutdown_wait_task in done:
        logger.info("application_shutdown_requested")
        if api_task is not None and api_task not in done:
            await _await_api_task_graceful_shutdown(
                api_task,
                orchestrator_task=orchestrator_task,
                shutdown_wait_task=shutdown_wait_task,
                shutdown_requested=shutdown_requested,
                api_server=api_server,
            )


async def _consume_completed_runtime_tasks(
    done: set[asyncio.Task],
    *,
    orchestrator_task: asyncio.Task[None],
    shutdown_wait_task: asyncio.Task[bool],
    api_task: asyncio.Task[None] | None,
    shutdown_requested: asyncio.Event,
    api_server: _EmbeddedApiServerContext,
) -> None:
    """Consume completed runtime tasks and raise the first non-cancellation failure."""
    failures: list[Exception] = []
    for task in (orchestrator_task, api_task, shutdown_wait_task):
        if task is None or task not in done:
            continue
        try:
            if api_task is not None and task is api_task:
                await _await_api_task_completion(
                    api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=api_server,
                )
                continue
            await task
        except asyncio.CancelledError:
            continue
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


async def _await_api_task_completion(
    api_task: asyncio.Task[None],
    *,
    shutdown_requested: asyncio.Event,
    api_server: _EmbeddedApiServerContext,
) -> None:
    """Await the API task and classify normal shutdown versus unexpected exit."""
    await api_task
    if shutdown_requested.is_set():
        logger.info("embedded_api_server_requested_application_shutdown", **api_server.log_context())
        return
    _raise_embedded_api_server_exit(
        api_server,
        reason="API task finished while application shutdown was not requested",
    )


async def _await_api_task_graceful_shutdown(
    api_task: asyncio.Task[None],
    *,
    orchestrator_task: asyncio.Task[None],
    shutdown_wait_task: asyncio.Task[bool],
    shutdown_requested: asyncio.Event,
    api_server: _EmbeddedApiServerContext,
) -> None:
    """Give Uvicorn a bounded window to run FastAPI lifespan shutdown."""
    grace_deadline = time.monotonic() + _EMBEDDED_API_SHUTDOWN_GRACE_SECONDS
    monitored_tasks: set[asyncio.Task] = {api_task, orchestrator_task}
    while api_task in monitored_tasks:
        timeout_seconds = grace_deadline - time.monotonic()
        if timeout_seconds <= 0:
            break
        done, _pending = await asyncio.wait(
            monitored_tasks,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            break
        await _consume_completed_runtime_tasks(
            done,
            orchestrator_task=orchestrator_task,
            shutdown_wait_task=shutdown_wait_task,
            api_task=api_task,
            shutdown_requested=shutdown_requested,
            api_server=api_server,
        )
        if api_task in done:
            return
        monitored_tasks.difference_update(done)

    logger.warning(
        "embedded_api_server_shutdown_timeout",
        **api_server.log_context(),
        timeout_seconds=_EMBEDDED_API_SHUTDOWN_GRACE_SECONDS,
    )


async def _cancel_task_if_pending(task: asyncio.Task | None) -> None:
    """Cancel and await one task only when it is still pending."""
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def main(
    log_level: str,
    runtime_paths: RuntimePaths,
    *,
    api: bool = True,
    api_port: int = 8765,
    api_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Main entry point for the multi-agent bot system."""
    storage_path = runtime_paths.storage_root

    # Configure logging before any background tasks or account setup begin.
    setup_logging(level=log_level, runtime_paths=runtime_paths)

    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials(runtime_paths=runtime_paths)

    # Ensure storage exists before any runtime components try to write into it.
    storage_path.mkdir(parents=True, exist_ok=True)

    logger.info("Starting orchestrator...")
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    set_runtime_starting()
    auxiliary_tasks: list[asyncio.Task] = []
    shutdown_requested = asyncio.Event()
    api_server = _EmbeddedApiServerContext(host=api_host, port=api_port)
    orchestrator_task: asyncio.Task[None] | None = None
    shutdown_wait_task: asyncio.Task[bool] | None = None
    api_task: asyncio.Task[None] | None = None

    try:
        auxiliary_specs = [
            (
                "config watcher",
                lambda: _watch_config_task(orchestrator.config_path, orchestrator),
                "config_watcher_supervisor",
            ),
            ("plugins watcher", lambda: watch_plugins_task(orchestrator), "plugins_watcher_supervisor"),
            ("skills watcher", lambda: _watch_skills_task(orchestrator), "skills_watcher_supervisor"),
        ]

        for task_name, operation, supervisor_name in auxiliary_specs:
            auxiliary_tasks.append(
                asyncio.create_task(
                    _run_auxiliary_task_forever(
                        task_name,
                        operation,
                        should_restart=lambda: not shutdown_requested.is_set(),
                    ),
                    name=supervisor_name,
                ),
            )

        if api:
            api_task = asyncio.create_task(
                _run_api_server(
                    api_host,
                    api_port,
                    log_level,
                    runtime_paths,
                    orchestrator.knowledge_refresh_scheduler,
                    shutdown_requested,
                ),
                name="api_server",
            )

        orchestrator_task = asyncio.create_task(orchestrator.start(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        await _wait_for_runtime_completion(
            orchestrator_task=orchestrator_task,
            shutdown_wait_task=shutdown_wait_task,
            api_task=api_task,
            shutdown_requested=shutdown_requested,
            api_server=api_server,
        )

    except KeyboardInterrupt:
        shutdown_requested.set()
        logger.info("Multi-agent bot system stopped by user")
    except Exception:
        shutdown_requested.set()
        logger.exception("Error in MindRoom runtime")
        raise
    finally:
        shutdown_requested.set()
        await _cancel_task_if_pending(shutdown_wait_task)
        await _cancel_task_if_pending(api_task)
        await _cancel_task_if_pending(orchestrator_task)
        # Cancel auxiliary supervisors before shutting down the orchestrator itself.
        for task in auxiliary_tasks:
            task.cancel()
        for task in auxiliary_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await orchestrator.stop()
        reset_matrix_sync_health()
        reset_runtime_state()
