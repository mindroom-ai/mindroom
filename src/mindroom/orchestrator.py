"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack, suppress
from contextvars import Context
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, NoReturn, cast, overload
from uuid import uuid4

import uvicorn

from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.agents import ensure_default_agent_workspaces
from mindroom.api.app_state import EmbeddedRuntime
from mindroom.api.startup_probes import serve_startup_probes
from mindroom.approval.manager import initialize_approval_store
from mindroom.approval.recovery import ApprovalRecovery
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.approval.transport import ApprovalMatrixTransport
from mindroom.attachments.storage import wait_for_attachment_cleanup_tasks
from mindroom.automations.runner import AutomationRunner
from mindroom.background_tasks import (
    SupervisedLoop,
    create_background_task,
    wait_for_background_tasks,
)
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.constants import ROUTER_AGENT_NAME, tracking_dir
from mindroom.delegation.recovery import cancel_approval_delegations
from mindroom.desktop.identity import controller_identity_for_live_bot
from mindroom.egress_broker.env import egress_broker_enabled
from mindroom.embeddings.embedder_health import check_embedder_health, handle_embedder_config_reload
from mindroom.event_loop_stall import (
    EventLoopStallDetector,
    shorten_gil_switch_interval,
    start_event_loop_stall_detector,
)
from mindroom.file_locks import release_file_lock, try_exclusive_file_lock
from mindroom.heap_probe import freeze_startup_heap, start_heap_tasks
from mindroom.hooks import (
    EVENT_CONFIG_RELOADED,
    ConfigReloadedContext,
    HookBindings,
    HookRegistry,
    HookRegistryLifetime,
    HookRegistryState,
    emit,
)
from mindroom.integrations import bind_integrations, unbind_integrations
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler, runtime_refresh_scheduler
from mindroom.knowledge.status import reconcile_knowledge_mode_transition_states
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.knowledge_graph.runtime import KnowledgeGraphRuntime
from mindroom.matrix.health import reset_matrix_sync_health
from mindroom.matrix.provisioning_heartbeat import run_provisioning_heartbeat
from mindroom.matrix.users import load_agent_user
from mindroom.matrix_rtc.call_manager import preload_matrix_call_dependencies
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import MemoryAutoFlushService
from mindroom.memory.legacy_team_memory import migrate_team_memory
from mindroom.providers.model_http_client import aclose_model_http_client
from mindroom.response_activity import ActiveResponseIdentity
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN, gather_shutdown_phase
from mindroom.runtime_state import (
    clear_api_server_address,
    reset_runtime_state,
    set_api_server_address,
    set_runtime_failed,
    set_runtime_ready,
    set_runtime_starting,
)
from mindroom.scheduling.runtime import ScheduledTaskRuntime
from mindroom.skill_learning.runner import SkillReviewRunner
from mindroom.startup_errors import EventJournalLostError, PermanentStartupError
from mindroom.startup_maintenance import StartupMaintenanceController
from mindroom.tool_system.plugins import (
    PluginGeneration,
    PluginReloadResult,
    config_plugins,
    dispose_plugins,
    prepare_plugins,
    publish_plugins,
)
from mindroom.tool_system.skills import clear_skill_cache, default_skill_roots, get_skill_snapshot
from mindroom.workers.runtime import (
    bind_worker_launch_config_source,
    lease_configured_primary_worker_manager,
    reset_primary_worker_manager,
    reset_worker_launch_config,
    shutdown_primary_worker_manager,
)

from . import file_watcher
from .bot import AgentBot, create_bot_for_entity
from .event_journal.opening import OpenEventJournal, bind_event_journal, open_event_journal
from .logging_config import get_logger, setup_logging
from .orchestration.accounts import prepare_managed_accounts, validate_entity_accounts
from .orchestration.computer_runtime import ComputerRuntimeCoordinator
from .orchestration.config_lifecycle import ConfigReloadLifecycle, load_published_config, load_startup_config
from .orchestration.config_updates import (
    catalog_change_restarts,
    catalog_refresh_restarts,
    configured_entity_names,
    mcp_dependents,
)
from .orchestration.config_warnings import warn_about_config_risks
from .orchestration.entity_fleet import EntityFleet, EntityFleetHooks, EntityStartResults
from .orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator
from .orchestration.plugin_watch import PluginWatchState, watch_plugins_task
from .orchestration.rooms import RoomSetup
from .orchestration.runtime import (
    is_permanent_startup_error,
    log_startup_phase_finished,
    log_startup_phase_started,
    run_shutdown_step,
    run_with_retry,
    wait_for_matrix_homeserver,
)
from .orchestration.script_runtime import ScriptRuntimeLifecycle, build_script_runtime, optional_script_gateway_url
from .orchestration.storage_preparation import prepare_runtime_storage
from .orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from .orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from .orchestration.turn_recovery import TurnJournalRecovery
from .thread_export.workspace_sync import WorkspaceThreadExportDeps, WorkspaceThreadExportRunner

if TYPE_CHECKING:
    import socket
    from collections.abc import Awaitable, Callable, Iterable, Mapping
    from pathlib import Path
    from types import FrameType

    import nio

    from mindroom.desktop.identity import DesktopControllerIdentity
    from mindroom.event_journal import ApprovalContinuation, ApprovalDeliveryView
    from mindroom.hooks import HookContextSupport

    from .config.main import Config
    from .constants import RuntimePaths
    from .event_journal import EventJournalStore
    from .orchestration.config_updates import ConfigUpdatePlan
    from .orchestration.services import RuntimeService
logger = get_logger(__name__)


_EMBEDDED_API_SHUTDOWN_GRACE_SECONDS = 5.0
_EVENT_JOURNAL_CHECK_SECONDS = 30.0


type _PluginWatchCapture = tuple[tuple[Path, ...], dict[Path, dict[Path, int]]]


@dataclass(frozen=True)
class _StagedPlugins:
    """Plugins a config reload loaded before anything stopped, with the watcher baselines taken first."""

    generation: PluginGeneration
    watch: _PluginWatchCapture


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


def _raise_embedded_api_server_exit(
    api_server: _EmbeddedApiServerContext,
    *,
    reason: str,
    cause: BaseException | None = None,
) -> NoReturn:
    """Raise the fatal lifecycle error for an unexpected API server exit."""
    logger.error(
        "fatal_embedded_api_server_exit",
        **api_server.log_context(),
        reason=reason,
        exc_info=(type(cause), cause, cause.__traceback__) if cause is not None else None,
    )
    msg = "Embedded API server exited unexpectedly"
    if cause is not None:
        raise RuntimeError(msg) from cause
    raise RuntimeError(msg)


def _raise_orchestrator_exit(*, reason: str) -> NoReturn:
    """Raise the fatal lifecycle error for an unexpected orchestrator exit."""
    logger.error("fatal_orchestrator_exit", reason=reason)
    msg = "MindRoom orchestrator exited unexpectedly"
    raise RuntimeError(msg)


class _SignalAwareUvicornServer(uvicorn.Server):
    """Uvicorn server that marks the shared shutdown event on signal exit."""

    def __init__(
        self,
        config: uvicorn.Config,
        shutdown_requested: asyncio.Event,
        *,
        on_started: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(config)
        self._shutdown_requested = shutdown_requested
        self._on_started = on_started
        self.received_signal_name: str | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Publish the API address only after Uvicorn successfully binds it."""
        await super().startup(sockets=sockets)
        if not self.started:
            return
        listeners = self.servers[0].sockets
        assert listeners is not None
        bound_address = cast(
            "tuple[str, int] | tuple[str, int, int, int]",
            listeners[0].getsockname(),
        )
        bound_host = bound_address[0]
        bound_port = bound_address[1]
        set_api_server_address(bound_host, bound_port)
        if self._on_started is not None:
            await self._on_started(bound_host, bound_port)
        logger.info("embedded_api_server_started", host=bound_host, port=bound_port)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Mirror Uvicorn signal handling and surface shutdown to the orchestrator."""
        del frame
        signal_number = int(sig)
        # A signal handler can interrupt a log write, which is not reentrant, so it
        # only records the signal; the serve loop's exit log reports it.
        self.received_signal_name = _signal_name(signal_number)
        self._shutdown_requested.set()
        if self.should_exit and signal_number == int(signal.SIGINT):
            self.force_exit = True
            return
        self.should_exit = True


@dataclass
class _MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    runtime_paths: RuntimePaths
    # The config startup loaded, then each one a reload publishes; every bot reads it here.
    config: Config
    api_enabled: bool = True
    fleet: EntityFleet = field(init=False)
    scheduled_tasks: ScheduledTaskRuntime = field(init=False)
    # The one journal every bot in this process borrows a store from, opened on
    # first use and closed after the last bot stops.
    _open_journal: OpenEventJournal | None = field(default=None, init=False, repr=False)
    _journal_held: bool = field(default=False, init=False, repr=False)
    running: bool = field(default=False, init=False)
    _memory_auto_flush: MemoryAutoFlushService = field(init=False, repr=False)
    # Services whose lifecycle follows the active config, in start order; they stop in reverse.
    _services: tuple[RuntimeService, ...] = field(init=False, repr=False)
    _skill_reviews: SkillReviewRunner = field(init=False, repr=False)
    _automations: AutomationRunner = field(init=False, repr=False)
    _budgets: BudgetMonitor = field(init=False, repr=False)
    _todo_poke_runtime: TodoPokeRuntimeCoordinator = field(init=False, repr=False)
    _knowledge_graph: KnowledgeGraphRuntime = field(init=False, repr=False)
    _tool_job_runtime: ToolJobRuntimeCoordinator = field(init=False, repr=False)
    _thread_export_runner: WorkspaceThreadExportRunner = field(init=False, repr=False)
    config_reload: ConfigReloadLifecycle = field(init=False)
    _mcp_manager: MCPServerManager | None = field(default=None, init=False)
    _config_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    turn_recovery: TurnJournalRecovery = field(init=False, repr=False)
    response_admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate, init=False)
    # The OpenAI-compatible requests the embedded API is serving, bound while it serves.
    openai_responses: set[ActiveResponseIdentity] = field(default_factory=set, init=False)
    _mcp_catalog_change_task_owner: object = field(default_factory=object, init=False, repr=False)
    _pending_mcp_catalog_restarts: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    plugin_watch: PluginWatchState = field(init=False)
    agent_cli_registry: TurnToolRegistry = field(default_factory=TurnToolRegistry, init=False)
    _knowledge_refresh_scheduler: KnowledgeRefreshScheduler = field(init=False)
    _knowledge_source_watcher: KnowledgeSourceWatcher = field(init=False)
    # The one holder of the active hook registry; the runtime, the scheduled-task runtime, and each bot generation
    # read it through a lifetime of their own that ends when they stop.
    hook_registry_state: HookRegistryState = field(
        default_factory=lambda: HookRegistryState(HookRegistry.empty()),
        init=False,
        repr=False,
    )
    _hook_lifetime: HookRegistryLifetime = field(init=False, repr=False)
    _runtime_shutdown_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _runtime_ready_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _router_reply_memberships_live_sync_ready: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )
    _computer_runtime: ComputerRuntimeCoordinator = field(init=False, repr=False)
    _external_trigger_runtime: ExternalTriggerRuntimeCoordinator = field(init=False, repr=False)
    _approval_transport: ApprovalMatrixTransport = field(init=False, repr=False)
    _startup_maintenance: StartupMaintenanceController = field(init=False, repr=False)
    _rooms: RoomSetup = field(init=False, repr=False)
    _script_runtime: ScriptRuntimeLifecycle = field(init=False, repr=False)
    agent_reply_memberships: AgentReplyMembershipIndex = field(
        default_factory=AgentReplyMembershipIndex,
        init=False,
        repr=False,
    )
    agent_reply_membership_sync: AgentReplyMembershipSync = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the runtime's collaborators around its paths and published config."""
        self._hook_lifetime = self.hook_registry_state.lifetime()
        self.fleet = EntityFleet(
            EntityFleetHooks(
                create_bots=self._create_bots,
                start_blocked=self._entities_blocked_by_failed_mcp_servers,
                entities_changing=self._entities_changing,
                entities_started=self._entities_started,
                setup_rooms_after_retry=self._setup_rooms_after_retry,
                reconcile_removed=self._reconcile_removed_entities,
                end_replies=self._end_entity_replies,
            ),
        )
        self.scheduled_tasks = ScheduledTaskRuntime(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            bots=lambda: self.agent_bots,
            hooks=self.hook_registry_state.lifetime(),
        )
        self._knowledge_refresh_scheduler = runtime_refresh_scheduler(self.runtime_paths)
        self._knowledge_source_watcher = KnowledgeSourceWatcher(self._knowledge_refresh_scheduler, self.runtime_paths)
        self.agent_reply_membership_sync = AgentReplyMembershipSync(
            self.agent_reply_memberships,
            lambda: self.agent_bots.values(),
        )
        self.plugin_watch = PluginWatchState(runtime_paths=self.runtime_paths)
        self.turn_recovery = TurnJournalRecovery(
            config=lambda: self.config,
            bots=lambda: self.agent_bots,
            first_sync_complete=self.entity_first_sync_complete,
            runtime_ready=self._runtime_ready_event.is_set,
        )
        self._computer_runtime = ComputerRuntimeCoordinator(
            runtime_paths=self.runtime_paths,
            api_enabled=self.api_enabled,
            agent_reply_memberships=self.agent_reply_memberships,
        )
        self._external_trigger_runtime = ExternalTriggerRuntimeCoordinator(api_enabled=self.api_enabled)
        self._skill_reviews = SkillReviewRunner(self.runtime_paths, lambda agent_name: self.agent_bots.get(agent_name))
        self._automations = AutomationRunner(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            bot_provider=lambda entity_name: self.agent_bots.get(entity_name),
            definition_provider=lambda name: self.hook_registry.automations.get(name),
            agent_reply_memberships=self.agent_reply_memberships,
        )
        self._budgets = BudgetMonitor(runtime_paths=self.runtime_paths, config_provider=lambda: self.config)
        self._knowledge_graph = KnowledgeGraphRuntime(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            redacted_event_ids=self.redacted_event_ids,
        )
        self._todo_poke_runtime = TodoPokeRuntimeCoordinator(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            bot_provider=lambda entity_name: self.agent_bots.get(entity_name),
            agent_reply_memberships=self.agent_reply_memberships,
        )
        self._tool_job_runtime = ToolJobRuntimeCoordinator(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            bot_provider=lambda entity_name: self.agent_bots.get(entity_name),
            agent_reply_memberships=self.agent_reply_memberships,
            journal_provider=self.event_journal_store,
        )
        self._thread_export_runner = WorkspaceThreadExportRunner(
            WorkspaceThreadExportDeps(
                runtime_paths=self.runtime_paths,
                config_provider=lambda: self.config,
                bot_provider=lambda entity_name: self.agent_bots.get(entity_name),
                response_admission_gate=self.response_admission_gate,
                agent_reply_memberships=self.agent_reply_memberships,
            ),
        )
        self._memory_auto_flush = MemoryAutoFlushService(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            redacted_event_ids=self.redacted_event_ids,
        )
        self._services = (
            self._memory_auto_flush,
            self._knowledge_source_watcher,
            self._knowledge_graph,
            self._todo_poke_runtime,
            self._automations,
            self._budgets,
            self._thread_export_runner,
        )
        self._script_runtime = build_script_runtime(
            self.runtime_paths,
            config_provider=lambda: self.config,
            bot_provider=lambda agent_name: self.agent_bots.get(agent_name),
            worker_lease_provider=lambda required_backend_locator: lease_configured_primary_worker_manager(
                self.runtime_paths,
                required_backend_locator=required_backend_locator,
            ),
            api_enabled=self.api_enabled,
        )
        self.config_reload = ConfigReloadLifecycle(
            runtime_paths=self.runtime_paths,
            is_running=lambda: self.running,
            current_config=lambda: self.config,
            agent_bots=lambda: self.agent_bots,
            load_config=partial(load_published_config, self.runtime_paths, api_enabled=self.api_enabled),
            apply_update_plan=self._apply_config_update_plan,
            response_admission_gate=self.response_admission_gate,
            before_runtime_replacement=self._thread_export_runner.prepare_runtime_replacement,
            rebind_published_config_consumers=lambda: self._bind_trigger_runtimes(),
            finish_reload=self._script_runtime.finish_reload,
            config_update_lock=self._config_update_lock,
        )
        self._approval_transport = ApprovalMatrixTransport(
            bot_provider=lambda agent_name: self.agent_bots.get(agent_name),
        )
        self._approval_recovery = ApprovalRecovery(
            deliver_unavailable_notice=self._approval_transport.deliver_unavailable_notice,
            journal_provider=self.event_journal_store,
            entity_configured=lambda name: name in self.config.agents or name in self.config.teams,
            entity_permanently_unavailable=self.fleet.is_disabled,
            recover_unavailable_final=self._recover_unavailable_final,
            cancel_delegations=lambda continuation, reason: cancel_approval_delegations(
                continuation,
                config=self.config,
                runtime_paths=self.runtime_paths,
                reason=reason,
            ),
        )
        self._rooms = RoomSetup(
            runtime_paths=self.runtime_paths,
            config_provider=lambda: self.config,
            router_client=self._router_client,
            refresh_agent_reply_memberships=lambda: self.refresh_agent_reply_memberships(),
        )
        self._startup_maintenance = StartupMaintenanceController(
            setup_rooms_and_memberships=self._setup_startup_rooms_and_memberships,
            sync_runtime_support=lambda config: self._sync_runtime_support_services(config),
            mark_runtime_support_ready=lambda: self._approval_recovery.mark_startup_runtime_support_ready(),
        )

    @property
    def agent_bots(self) -> Mapping[str, AgentBot]:
        """Return every managed bot by entity name; the fleet owns their lifecycle."""
        return self.fleet.bots

    @property
    def hook_registry(self) -> HookRegistry:
        """Return the active hook registry for the runtime's own hooks, which hold none once it stops."""
        return self._hook_lifetime.registry

    @property
    def script_runtime(self) -> ScriptRuntimeLifecycle:
        """Return the process-local background-script lifecycle collaborator."""
        return self._script_runtime

    async def _no_live_work(self) -> bool:
        """Return whether no work that ``mindroom check-active-responses`` counts is live; unknown counts as live."""
        try:
            script_runs = await self._script_runtime.active_runs()
        except Exception:
            logger.warning("shutdown_activity_unknown", exc_info=True)
            return False
        return not (
            any(not run.recoverable for run in script_runs)
            or self.response_admission_gate.in_flight_response_count
            or self.openai_responses
            or self.active_call_identities()
        )

    def active_call_identities(self) -> list[ActiveResponseIdentity]:
        """Return one identity per voice call that any bot has joined or is joining."""
        return [
            ActiveResponseIdentity(responder=entity_name, requester_id=requester_id)
            for entity_name, bot in self.agent_bots.items()
            for requester_id in bot.active_call_requesters
        ]

    @property
    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler:
        """Return the orchestrator-owned background knowledge refresh scheduler."""
        return self._knowledge_refresh_scheduler

    @property
    def skill_reviews(self) -> SkillReviewRunner:
        """Return the orchestrator-owned runner of automatic skill reviews."""
        return self._skill_reviews

    @property
    def automations(self) -> AutomationRunner:
        """Return the orchestrator-owned runner of automations."""
        return self._automations

    @property
    def budgets(self) -> BudgetMonitor:
        """Return the orchestrator-owned monitor of per-user spending budgets."""
        return self._budgets

    def running_entity_names(self) -> frozenset[str]:
        """Return the entities whose current bot generation is running."""
        return frozenset(entity_name for entity_name, bot in self.agent_bots.items() if bot.running)

    def entity_first_sync_complete(self, entity_name: str) -> bool | None:
        """Return first-sync readiness for the current entity generation."""
        bot = self.agent_bots.get(entity_name)
        if bot is None:
            return None
        return bot.running and bot.first_sync_complete

    def desktop_controller_identity(self, entity_name: str) -> DesktopControllerIdentity:
        """Resolve the current running bot's already-owned Matrix device pin."""
        return controller_identity_for_live_bot(
            entity_name,
            self.agent_bots.get(entity_name),
        )

    def _reset_runtime_shutdown_event(self) -> asyncio.Event:
        """Create the shutdown event for the current orchestrator run."""
        shutdown_event = asyncio.Event()
        self._runtime_shutdown_event = shutdown_event
        return shutdown_event

    async def send_approval_notice(
        self,
        *,
        room_id: str,
        approval_event_id: str,
        thread_id: str | None,
        reason: str,
    ) -> bool:
        """Send one approval notice through the public runtime protocol."""
        return await self._approval_transport.send_notice(
            room_id=room_id,
            approval_event_id=approval_event_id,
            thread_id=thread_id,
            reason=reason,
        )

    async def _recover_unavailable_final(
        self,
        principal_id: str,
        continuation: ApprovalContinuation,
    ) -> bool:
        """Finalize frozen success as its original Matrix principal, without model startup."""
        bot = self.agent_bots.get(continuation.entity_name)
        if bot is not None and bot.journal_principal().principal_id != principal_id:
            bot = None
        if bot is None:
            agent_user = load_agent_user(continuation.entity_name, self.runtime_paths)
            if agent_user is None:
                logger.error(
                    "approval_original_principal_account_missing",
                    approval_id=continuation.approval_id,
                    entity_name=continuation.entity_name,
                    principal_id=principal_id,
                )
                return False
            # Built like any managed bot, but with no rooms: the entity may no longer be configured.
            bot = AgentBot(agent_user, rooms=[], orchestrator=self)
            if bot.journal_principal().principal_id != principal_id:
                logger.error(
                    "approval_original_principal_identity_mismatch",
                    approval_id=continuation.approval_id,
                    expected_principal_id=principal_id,
                    actual_principal_id=bot.journal_principal().principal_id,
                )
                return False

        return await bot.recover_approval_final(continuation.approval_id)

    async def leave_matrix_room(self, agent_name: str, room_id: str) -> bool:
        """Route dashboard departures through the current bot's durable membership owner."""
        if not self.response_admission_gate.admit():
            msg = "MindRoom is starting or reloading; retry the room departure"
            raise RuntimeError(msg)
        try:
            bot = self.agent_bots.get(agent_name)
            if bot is None or not bot.running or bot.client is None:
                msg = f"No running Matrix owner for {agent_name}"
                raise RuntimeError(msg)
            return await bot.change_local_membership(room_id, "leave")
        finally:
            self.response_admission_gate.release()

    def _approval_cards(self) -> ApprovalDeliveryView | None:
        """Return the router principal's approval-card store, once it exists.

        Bound before the router bot is built as well as after, because the
        transport is configured on every config reload. The store belongs to
        the bot, so the early call has nothing to give; the manager treats a
        missing store as "no card is recoverable", and the rebind that follows
        bot construction supplies the real one.
        """
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        return None if router_bot is None else router_bot.journal_principal()

    def _bind_started_runtime_support_services(self) -> None:
        """Bind current runtime support objects needed by live callbacks."""
        self._configure_approval_store_transport()
        self._thread_export_runner.queue_full_pass()

    async def _start_runtime_support(self) -> None:
        """Bind live-callback support, then start the job and script runtimes that deliver through the started bots."""
        self._bind_started_runtime_support_services()
        await self._tool_job_runtime.sync()
        await self._script_runtime.start()

    async def _setup_startup_rooms_and_memberships(self, bots: list[AgentBot]) -> None:
        """Run startup room setup, then publish trigger delivery runtime."""
        await self._set_up_rooms_then_bind(bots, retry_step="Setting up Matrix rooms and memberships")
        self._bind_integrations()

    async def _set_up_rooms_then_bind(self, bots: list[AgentBot], *, retry_step: str | None = None) -> None:
        """Reconcile the rooms of bots that just started, then bind trigger delivery to whatever now runs.

        With ``retry_step`` the setup retries until it succeeds, which suits work nothing waits on; without it, a
        failure propagates to the caller holding the config-update lock. A failure still binds first, so a
        replacement whose setup failed does not leave delivery unbound; cancellation does not, so a shutdown's
        withdrawal stands.
        """
        try:
            if bots and retry_step is not None:
                await run_with_retry(
                    retry_step,
                    partial(self._rooms.setup, bots),
                    permanent_error_check=is_permanent_startup_error,
                    update_runtime_state=False,
                )
            elif bots:
                await self._rooms.setup(bots)
        except Exception:
            self._bind_trigger_runtimes()
            raise
        self._bind_trigger_runtimes()

    def _bind_integrations(self) -> None:
        """Let integration events reach the live agent bots; `self.agent_bots` stays current, so one binding lasts."""
        if not self.api_enabled:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        bind_integrations(api_main.app, self.agent_bots, self.agent_reply_memberships)

    def _unbind_integrations(self) -> list[tuple[str, object]]:
        """Refuse new integration events and return each unbound runtime by name, which owns its running deliveries."""
        if not self.api_enabled:
            return []
        from mindroom.api import main as api_main  # noqa: PLC0415

        return unbind_integrations(api_main.app)

    def _configure_approval_store_transport(self) -> None:
        """Bind approval transport hooks to the current shared runtime services."""
        initialize_approval_store(
            self.runtime_paths,
            transport=self._approval_transport,
            cards=self._approval_cards(),
            recovery=self._approval_recovery,
        )

    def _running_startup_maintenance_bots(self) -> list[AgentBot]:
        """Return currently running bots in startup-maintenance order."""
        return self._running_bots_for_entities(
            (ROUTER_AGENT_NAME, *(name for name in self.agent_bots if name != ROUTER_AGENT_NAME)),
        )

    def _running_bots_for_entities(self, entity_names: Iterable[str]) -> list[AgentBot]:
        """Return running bots for the given entity names."""
        running_bots: list[AgentBot] = []
        for entity_name in entity_names:
            bot = self.agent_bots.get(entity_name)
            if bot is not None and bot.running:
                running_bots.append(bot)
        return running_bots

    async def _sync_runtime_support_services(self, config: Config, *, previous_config: Config | None = None) -> None:
        """Bring every runtime service in line with the active config."""
        if previous_config is not None:
            await asyncio.to_thread(
                reconcile_knowledge_mode_transition_states,
                previous_config,
                config,
                self.runtime_paths,
            )
        ensure_default_agent_workspaces(config, self.runtime_paths.storage_root)
        self._configure_approval_store_transport()
        for service in self._services:
            await service.sync(config)
        await self._tool_job_runtime.sync()

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
                on_catalog_change=self._notify_mcp_catalog_change,
            )
            self._mcp_manager = manager
        bind_mcp_server_manager(manager)
        return await manager.sync_servers(config)

    def _entities_blocked_by_failed_mcp_servers(self, entity_names: set[str]) -> set[str]:
        """Return entities blocked because a required MCP server is currently unavailable."""
        manager = self._mcp_manager
        if manager is None:
            return set()
        failed_server_ids = manager.failed_required_server_ids()
        if not failed_server_ids:
            return set()
        return mcp_dependents(self.config, failed_server_ids) & entity_names

    def _log_mcp_degraded_entities(self, config: Config) -> None:
        """Warn once per unavailable optional MCP server about entities running without its tools."""
        manager = self._mcp_manager
        if manager is None:
            return
        running_entities = self.running_entity_names()
        for server_id in sorted(manager.failed_server_ids() - manager.failed_required_server_ids()):
            degraded_entities = mcp_dependents(config, {server_id}) & running_entities
            if not degraded_entities:
                continue
            logger.warning(
                "Entities running without tools from unavailable MCP server",
                server_id=server_id,
                degraded_entities=sorted(degraded_entities),
            )

    def validate_managed_entity_identities(self) -> None:
        """Validate persisted managed Matrix identities for the live config."""
        validate_entity_accounts(self.config, self.runtime_paths)

    async def _create_bots(self, entity_names: set[str]) -> dict[str, AgentBot]:
        """Prepare Matrix accounts, then build bots for entities the published config configures."""
        entity_users = await prepare_managed_accounts(
            self.config,
            self.runtime_paths,
            sorted(entity_names),
            include_internal_user=False,
            update_runtime_state=not self.running,
        )
        return {
            entity_name: create_bot_for_entity(entity_name, entity_users[entity_name], self)
            for entity_name in sorted(entity_names)
        }

    def observe_room_activity(self, room_id: str) -> None:
        """Let thread exports know that a bot admitted a conversation event in one room."""
        self._thread_export_runner.mark_room_activity(room_id)

    async def _entities_changing(self, entity_names: set[str]) -> None:
        """Withdraw capabilities bound to these entities before any of them stops."""
        self._external_trigger_runtime.unbind()
        self._computer_runtime.unbind()
        if ROUTER_AGENT_NAME in entity_names:
            # Fence room-backed grants before stopping their sole control client.
            self.agent_reply_membership_sync.invalidate(self.config, reason="router_replacement")
        await self.scheduled_tasks.release(entity_names)

    async def _reconcile_removed_entities(self, entity_names: set[str]) -> set[str]:
        """Settle removed entities' approvals and scheduled tasks while their bots exist; return unresolved approvals."""
        await self.scheduled_tasks.fail_removed(entity_names)
        return await self._approval_recovery.reconcile_removed_entities(entity_names)

    async def _entities_started(self, results: EntityStartResults) -> None:
        """Publish one batch of start outcomes to the runtime services that depend on live bots."""
        self._bind_started_runtime_support_services()
        await self._approval_recovery.reconcile_unavailable_entities(results.permanently_failed_entities)
        self.turn_recovery.schedule()

    async def _setup_rooms_after_retry(self, entity_name: str) -> None:
        """Reconcile rooms after a background retry started one entity, and every bot's rooms after the router started."""
        bots_to_setup = self._running_bots_for_entities(
            self.agent_bots if entity_name == ROUTER_AGENT_NAME else (entity_name,),
        )
        await self._set_up_rooms_then_bind(
            bots_to_setup,
            retry_step=f"Updating Matrix room memberships for {entity_name}",
        )

    def _bind_trigger_runtimes(self) -> None:
        """Bind external-trigger and computer delivery once their router and targets run, never after shutdown began."""
        if not self.running:
            return
        self._external_trigger_runtime.bind_if_ready(self.config, self.agent_bots)
        self._computer_runtime.bind_if_ready(self.config, self.agent_bots)

    async def _end_entity_replies(self, entity_names: set[str]) -> None:
        """End the journal replies of removed entities; no bot remains to finish them."""
        await self.event_journal_store().end_entity_replies(entity_names.__contains__, now_ns=time.time_ns())

    def _commit_plugins(
        self,
        generation: PluginGeneration,
        config: Config,
        watch: _PluginWatchCapture,
    ) -> PluginReloadResult:
        """Publish ``generation``, make its hooks for ``config`` live, and adopt the watcher baselines taken before it loaded."""
        result = publish_plugins(generation)
        self.hook_registry_state.registry = config_plugins(config, self.runtime_paths).hooks
        self.plugin_watch.replace_snapshots(*watch)
        reset_worker_launch_config()
        for skipped in result.skipped:
            logger.warning("Plugin skipped", plugin_path=skipped.path, error=str(skipped.error))
        return result

    @overload
    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
    ) -> PluginReloadResult: ...

    @overload
    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
        expected_revision: int,
    ) -> PluginReloadResult | None: ...

    async def reload_plugins_now(
        self,
        *,
        source: str,
        changed_paths: tuple[Path, ...] = (),
        expected_revision: int | None = None,
    ) -> PluginReloadResult | None:
        """Rebuild and atomically swap the live plugin registry snapshot."""
        if not self.running:
            msg = "Plugin reload unavailable until startup finishes."
            raise RuntimeError(msg)
        async with self._config_update_lock:
            if expected_revision is not None and self.plugin_watch.revision != expected_revision:
                logger.info(
                    "Skipping stale watcher plugin reload",
                    expected_revision=expected_revision,
                    current_revision=self.plugin_watch.revision,
                )
                return None
            config = self.config
            await self._script_runtime.interrupt_for_plugin_reload()
            logger.info(
                "Reloading plugins",
                source=source,
                changed_paths=[str(path) for path in changed_paths],
            )
            watch = self.plugin_watch.capture(config)
            try:
                try:
                    generation = prepare_plugins(config, self.runtime_paths)
                except Exception as exc:
                    logger.warning(
                        "Plugin reload failed; the previous plugins stay active",
                        source=source,
                        error=str(exc),
                    )
                    raise
                result = self._commit_plugins(generation, config, watch)
                logger.info(
                    "Plugin reload complete",
                    source=source,
                    active_plugins=list(result.active_plugin_names),
                    cancelled_task_count=result.cancelled_task_count,
                )
                return result
            finally:
                await self._script_runtime.finish_reload()

    def _publish_config(self, plan: ConfigUpdatePlan, staged: _StagedPlugins | None) -> None:
        """Make the new config, its plugins, and its hook registry live in one step, with what running bots derive."""
        new_config = plan.new_config
        self.config = new_config
        if plan.reply_membership_policy_changed:
            self.agent_reply_membership_sync.invalidate(new_config, reason="config_reload")
        if staged is None:
            # Equal plugin entries compose the hooks already live.
            self.plugin_watch.sync_roots(new_config)
            reset_worker_launch_config()
        else:
            self._commit_plugins(staged.generation, new_config, staged.watch)
        for bot in self.agent_bots.values():
            bot.apply_published_config()

    def _stage_plugins(self, plan: ConfigUpdatePlan) -> _StagedPlugins | None:
        """Load changed plugins before anything stops, so a plugin set that cannot load fails the reload with every bot intact."""
        if not plan.plugin_changes:
            return None
        watch = self.plugin_watch.capture(plan.new_config)
        return _StagedPlugins(prepare_plugins(plan.new_config, self.runtime_paths), watch)

    async def initialize(self) -> None:
        """Initialize all managed bots from the config startup loaded."""
        set_runtime_starting("Preparing agents")
        logger.info("Initializing multi-agent system...")

        config = self.config
        await preload_matrix_call_dependencies(config)
        freeze_startup_heap()
        watch = self.plugin_watch.capture(config)
        generation = prepare_plugins(config, self.runtime_paths)
        try:
            entity_names = configured_entity_names(config)
            entity_users = await prepare_managed_accounts(
                config,
                self.runtime_paths,
                entity_names,
                include_internal_user=True,
                update_runtime_state=not self.running,
            )
            self.agent_reply_memberships.invalidate(config, reason="initial_config")
            await self._bind_event_journal()
            await self._tool_job_runtime.initialize()
        except BaseException:
            dispose_plugins(generation)
            raise
        self._commit_plugins(generation, config, watch)
        warn_about_config_risks(config, self.runtime_paths)
        await self._sync_mcp_manager(config)
        self._configure_approval_store_transport()
        for entity_name in entity_names:
            self.fleet.add(entity_name, create_bot_for_entity(entity_name, entity_users[entity_name], self))

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots from the config startup loaded and publish readiness state."""
        try:
            await self._start_runtime()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            set_runtime_failed(str(exc))
            raise

    async def wait_until_runtime_ready(self) -> None:
        """Return once startup has published runtime readiness."""
        await self._runtime_ready_event.wait()

    async def _start_router_bot(self) -> AgentBot:
        """Start the router bot, retrying until it succeeds."""
        if self._entities_blocked_by_failed_mcp_servers({ROUTER_AGENT_NAME}):
            msg = "Router bot depends on unavailable MCP servers"
            raise RuntimeError(msg)
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        if router_bot is None:
            msg = "Router bot is required for startup"
            raise RuntimeError(msg)

        set_runtime_starting("Starting router Matrix account")
        await run_with_retry(
            "Starting router Matrix account",
            router_bot.start,
            permanent_error_check=is_permanent_startup_error,
        )
        return router_bot

    def router_hook_context(self) -> HookContextSupport | None:
        """Return the router bot's hook support, whose client backs what another bot's own cannot."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        return None if router_bot is None else router_bot.hook_context

    def running_entity_client(self, entity_name: str) -> nio.AsyncClient | None:
        """Return the Matrix client of one running managed entity."""
        bot = self.agent_bots.get(entity_name)
        if bot is None or not bot.running:
            return None
        return bot.client

    async def open_reply_event_id(self, entity_name: str, room_id: str, thread_id: str | None) -> str | None:
        """Return the event of one running managed entity's unfinished reply in one conversation."""
        bot = self.agent_bots.get(entity_name)
        if bot is None or not bot.running:
            return None
        return await bot.journal_principal().replies.open_event_id(room_id, thread_id)

    async def redacted_event_ids(
        self,
        entity_name: str,
        room_id: str,
        event_ids: tuple[str, ...],
    ) -> frozenset[str] | None:
        """Return which of a room's events one managed entity saw redacted, or None until it is running and synced.

        Redactions made while the entity was stopped reach its journal only with its first sync.
        """
        bot = self.agent_bots.get(entity_name)
        if bot is None or not bot.running or not bot.first_sync_complete:
            return None
        return await bot.redacted_event_ids(room_id, event_ids)

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

    async def handle_bot_ready(self, bot: AgentBot) -> None:
        """Handle bot-ready notifications through the public runtime protocol."""
        if bot.agent_name == ROUTER_AGENT_NAME:
            self._router_reply_memberships_live_sync_ready.set()
        if bot.agent_name == ROUTER_AGENT_NAME and bot.running and bot.client is not None:
            await self._approval_recovery.mark_router_ready()
        if self.config.personal_agent_instance(bot.agent_name) is not None:
            router = self.agent_bots.get(ROUTER_AGENT_NAME)
            if router is not None:
                router.personal_room_target_ready()
        self.turn_recovery.schedule()

    async def refresh_agent_reply_memberships(self) -> None:
        """Refresh room-backed reply grants through the router's Matrix client."""
        config = self.config
        router_client = self._router_client()
        if router_client is None:
            self.agent_reply_membership_sync.invalidate(config, reason="router_unavailable")
            return
        await self.agent_reply_memberships.refresh(config, self.runtime_paths, router_client)
        self.turn_recovery.schedule()
        await self.agent_reply_membership_sync.apply_grant_change()

    async def _end_unconfigured_entity_replies(self, config: Config) -> None:
        """End the replies of entities removed while MindRoom was stopped; no bot finishes them."""
        configured = set(configured_entity_names(config))
        await self.event_journal_store().end_entity_replies(
            lambda entity_name: entity_name not in configured,
            now_ns=time.time_ns(),
        )

    async def _start_runtime(self) -> None:
        """Run the startup sequence before handing off to the sync loops."""
        runtime_shutdown_event = self._reset_runtime_shutdown_event()
        self._runtime_ready_event.clear()
        self._router_reply_memberships_live_sync_ready.clear()
        self._approval_recovery.reset_startup_cleanup_gate()
        phase_started = log_startup_phase_started("wait_for_matrix_homeserver")
        await wait_for_matrix_homeserver(runtime_paths=self.runtime_paths)
        log_startup_phase_finished("wait_for_matrix_homeserver", phase_started)

        if not self.agent_bots:
            phase_started = log_startup_phase_started("initialize_runtime")
            await self.initialize()
            log_startup_phase_finished("initialize_runtime", phase_started)

        phase_started = log_startup_phase_started("start_router_bot")
        router_bot = await self._start_router_bot()
        log_startup_phase_finished("start_router_bot", phase_started)

        set_runtime_starting("Starting remaining Matrix bot accounts")
        phase_started = log_startup_phase_started("start_remaining_bots")
        start_results = await self.fleet.try_start(self.fleet.names - {ROUTER_AGENT_NAME})
        log_startup_phase_finished("start_remaining_bots", phase_started)

        started_bots = [router_bot, *start_results.started_bots]
        self._log_degraded_startup(
            [*start_results.retryable_entities, *start_results.permanently_failed_entities],
        )

        config = self.config
        await self._end_unconfigured_entity_replies(config)
        self._log_mcp_degraded_entities(config)
        self._rooms.resolve_aliases(started_bots, config)
        phase_started = log_startup_phase_started("bind_runtime_support")
        await self._start_runtime_support()
        log_startup_phase_finished("bind_runtime_support", phase_started)

        async with self.config_reload.startup_publication_admission():
            self.running = True

            # Owned membership commands need the source and admission pump.
            # Semantic dispatch stays parked until initial runtime publication.
            set_runtime_starting("Starting Matrix ingestion loops")
            phase_started = log_startup_phase_started("start_matrix_sync_loops")
            self.fleet.start_sync(started_bots)
            log_startup_phase_finished("start_matrix_sync_loops", phase_started)

            self._startup_maintenance.start(started_bots, config)
            room_membership_policy_configured = self.agent_reply_memberships.needs_refresh(config)
            if room_membership_policy_configured:
                set_runtime_starting("Waiting for the router's first Matrix sync")

            if runtime_shutdown_event.is_set():
                return

            # Publish semantic callbacks once the router's initial owned sync has
            # rebuilt room-backed reply grants from Matrix. Room setup keeps
            # running in the background: a grant room it has not joined or
            # created yet stays pending, and its sources replay after setup
            # refreshes the grants.
            sync_ready = await self._wait_for_initial_membership_sync(
                runtime_shutdown_event,
                room_membership_policy_configured=room_membership_policy_configured,
            )
            if not sync_ready:
                return

            self.fleet.schedule_retries(start_results.retryable_entities)

            create_background_task(
                check_embedder_health(config, self.runtime_paths, reason="startup"),
                name="embedder_startup_health_check",
            )
            self.config_reload.startup_published(config)
            set_runtime_ready()
            self._runtime_ready_event.set()
            self.turn_recovery.schedule()
        # Stay alive until explicit shutdown. Hot reload replaces the fleet's sync
        # tasks, so awaiting the initial task generation would let a
        # config-triggered restart look like normal orchestrator completion.
        await runtime_shutdown_event.wait()

    async def _wait_for_initial_membership_sync(
        self,
        runtime_shutdown_event: asyncio.Event,
        *,
        room_membership_policy_configured: bool,
    ) -> bool:
        """Keep semantic dispatch parked until the router has observed owned sync."""
        if not room_membership_policy_configured:
            return True

        assert self.response_admission_gate.closed, "startup publication must own response admission"
        return await self._wait_for_router_reply_memberships_or_shutdown(runtime_shutdown_event)

    async def _wait_for_router_reply_memberships_or_shutdown(
        self,
        runtime_shutdown_event: asyncio.Event,
    ) -> bool:
        """Wait until router sync closes the startup snapshot gap, or shutdown wins."""
        ready_wait = asyncio.create_task(self._router_reply_memberships_live_sync_ready.wait())
        shutdown_wait = asyncio.create_task(runtime_shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_wait, shutdown_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            return (
                shutdown_wait not in done
                and ready_wait in done
                and self._router_reply_memberships_live_sync_ready.is_set()
            )
        finally:
            for task in (ready_wait, shutdown_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ready_wait, shutdown_wait, return_exceptions=True)

    async def _refresh_running_bots_presence(self) -> None:
        """Let each bot this reload keeps running show its presence under the published config."""
        for entity_name, bot in self.agent_bots.items():
            try:
                await bot.refresh_presence()
            except Exception:
                logger.exception("bot_presence_update_failed", agent=entity_name)
            logger.debug("bot_config_updated", agent=entity_name)

    async def _emit_config_reloaded(self, plan: ConfigUpdatePlan, changed_entities: set[str]) -> None:
        """Emit the config:reloaded observer event after applying a new snapshot."""
        if not self.hook_registry.has_hooks(EVENT_CONFIG_RELOADED):
            return

        router = self.router_hook_context()
        bindings = HookBindings(runtime_started_at=time.time()) if router is None else router.bindings()
        context = ConfigReloadedContext(
            # The router's Matrix capabilities, under the runtime's hook lifetime rather than the router bot's.
            **replace(bindings, registry_state=self._hook_lifetime).context_kwargs(
                EVENT_CONFIG_RELOADED,
                config=plan.new_config,
                runtime_paths=self.runtime_paths,
                correlation_id=f"config-reload:{uuid4().hex}",
            ),
            changed_entities=tuple(sorted(changed_entities)),
            added_entities=tuple(sorted(plan.start)),
            removed_entities=tuple(sorted(plan.remove)),
            plugin_changes=plan.plugin_changes,
        )
        await emit(self.hook_registry, EVENT_CONFIG_RELOADED, context)

    async def _handle_mcp_catalog_change(self, server_id: str) -> None:
        """Restart entities that reference one changed MCP catalog."""
        try:
            if not self.running:
                return
            if not catalog_change_restarts(self.config, {server_id}, self.fleet.names):
                return
            await self.config_reload.apply_with_response_admission(
                partial(self._apply_mcp_catalog_change, server_id),
                operation_name="MCP catalog restart",
                request_is_current=lambda: self.running,
            )
        finally:
            # A restart that ends without applying must not absorb later changes.
            if self._pending_mcp_catalog_restarts.get(server_id) is asyncio.current_task():
                del self._pending_mcp_catalog_restarts[server_id]

    async def _notify_mcp_catalog_change(self, server_id: str) -> None:
        """Schedule a catalog restart so an admitted MCP call can release first."""
        # A queued restart reads the catalog only when it applies, so it already covers this change.
        if not self.running or server_id in self._pending_mcp_catalog_restarts:
            return
        self._pending_mcp_catalog_restarts[server_id] = create_background_task(
            self._handle_mcp_catalog_change(server_id),
            name=f"mcp_catalog_change:{server_id}",
            owner=self._mcp_catalog_change_task_owner,
            # A tool call can report the change, and the bots this restarts must not inherit its turn's context.
            context=Context(),
        )

    async def _apply_mcp_catalog_change(self, server_id: str) -> None:
        """Apply one MCP catalog-triggered entity replacement."""
        async with self._config_update_lock:
            # This restart reads the catalog next, so changes reported from here on need their own restart.
            self._pending_mcp_catalog_restarts.pop(server_id, None)
            if not self.running:
                return
            config = self.config
            changed_entities = catalog_change_restarts(config, {server_id}, self.fleet.names)
            if not changed_entities:
                return
            logger.info(
                "Restarting entities after MCP catalog change",
                server_id=server_id,
                entities=sorted(changed_entities),
            )
            async with self._paused_startup_maintenance():
                start_results = await self.fleet.replace(changed_entities)
                await self._set_up_rooms_then_bind(start_results.started_bots)
                if start_results.permanently_failed_entities:
                    logger.warning(
                        "MCP catalog restart left some bots disabled",
                        server_id=server_id,
                        entities=start_results.permanently_failed_entities,
                    )

    def _paused_startup_maintenance(self) -> AbstractAsyncContextManager[None]:
        """Pause startup maintenance across a runtime replacement, then replay it with the bots running after it."""
        return self._startup_maintenance.paused(
            config_provider=lambda: self.config if self.running else None,
            running_bots=self._running_startup_maintenance_bots,
        )

    async def _reconcile_post_update_rooms(
        self,
        plan: ConfigUpdatePlan,
        changed_entities: set[str],
    ) -> None:
        """Reconcile rooms and memberships after entity/config updates."""
        bots_to_setup = self._running_bots_for_entities(changed_entities | plan.reconcile_rooms)
        if bots_to_setup or plan.mindroom_user_changed or plan.room_access_changed or plan.authorization_changed:
            await self._rooms.setup(bots_to_setup)
        elif plan.matrix_space_changed:
            await self._rooms.link_root_space(await self._rooms.ensure_rooms_exist())

    async def _finalize_config_reload(
        self,
        current_config: Config,
        plan: ConfigUpdatePlan,
        changed_entities: set[str],
    ) -> None:
        """Publish post-reload runtime services and external trigger delivery binding."""
        handle_embedder_config_reload(current_config, plan.new_config, self.runtime_paths)
        await self._sync_runtime_support_services(plan.new_config, previous_config=current_config)
        await self._approval_recovery.mark_startup_runtime_support_ready()
        self._bind_trigger_runtimes()
        await self._emit_config_reloaded(plan, changed_entities)

    async def _apply_config_update_plan(self, current_config: Config, plan: ConfigUpdatePlan) -> bool:
        """Apply one computed config update plan: stop what changes, publish the new config, then start."""
        new_config = plan.new_config
        # Accounts are ready, and their names checked, before anything stops or the config publishes.
        if plan.start or plan.restart or plan.mindroom_user_changed:
            await prepare_managed_accounts(
                new_config,
                self.runtime_paths,
                sorted(plan.start | plan.restart),
                include_internal_user=plan.mindroom_user_changed,
                update_runtime_state=not self.running,
            )
        await self._script_runtime.apply_update_plan(plan)
        try:
            async with self._paused_startup_maintenance():
                staged = self._stage_plugins(plan)
                try:
                    # Removal first, while the current router still delivers its approval notices
                    # and before a replacement router's orphan cleanup could kick the removed accounts.
                    unsettled_removals = await self.fleet.remove(plan.remove)
                    await self.fleet.stop(plan.restart)
                except BaseException:
                    if staged is not None:
                        dispose_plugins(staged.generation)
                    raise
                self._publish_config(plan, staged)
                # Now that the removal is published, recovery can judge the removed owners as gone.
                await self._approval_recovery.reconcile_unavailable_entities(unsettled_removals)
                await self._script_runtime.reopen_script_calls()
                catalog_changed = await self._sync_mcp_manager(new_config)
                logger.info(
                    "updating_config_authorization",
                    platform_administrator_ids=new_config.administrators,
                )
                warn_about_config_risks(new_config, self.runtime_paths)
                self._computer_runtime.unbind()
                catalog_restarts, deferred_catalog_servers = catalog_refresh_restarts(
                    plan,
                    catalog_changed,
                    self.fleet.names,
                )
                for deferred_server_id in deferred_catalog_servers:
                    # Replies may still be running, so these dependents restart behind their own drain.
                    await self._notify_mcp_catalog_change(deferred_server_id)
                await self.fleet.stop(catalog_restarts)
                await self._refresh_running_bots_presence()
                if plan.router_invite_policy_changed and ROUTER_AGENT_NAME not in plan.restart:
                    router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
                    if router_bot is not None:
                        router_bot.schedule_pending_invite_reconciliation()

                changed_entities = set(plan.restart | plan.start | catalog_restarts)
                await self.fleet.create(changed_entities)
                start_results = await self.fleet.start(changed_entities)
                await self._reconcile_post_update_rooms(plan, changed_entities)
                if start_results.permanently_failed_entities:
                    logger.warning(
                        "Configuration update left some bots disabled due to permanent startup errors",
                        agent_names=start_results.permanently_failed_entities,
                    )

                await self._finalize_config_reload(current_config, plan, changed_entities | plan.live_updated)
                logger.info("configuration_update_complete", affected_bot_count=len(changed_entities))
                return bool(changed_entities)
        finally:
            await self._script_runtime.reopen_script_calls()

    def _router_client(self) -> nio.AsyncClient | None:
        """Return the router's Matrix client when the router runs one."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        client = None if router_bot is None else router_bot.client
        if client is None:
            logger.warning("Router client not available")
        return client

    async def _bind_event_journal(self) -> None:
        """Refuse startup unless the configured database is this install's journal.

        Runs before any bot is created, so a store this install is not bound to
        is never written to: opening a stranger's journal would take over its
        outbox and recovery work, and opening a fresh one would replay and
        re-answer a past that already happened.
        """
        config = self.config
        store = self.event_journal_store()
        identity = await bind_event_journal(
            store,
            journal_config=config.event_journal,
            runtime_paths=self.runtime_paths,
            storage_path=self.runtime_paths.storage_root,
        )
        # Storage roots bound to one database share its identity: a second
        # runtime against it would answer every message again.
        if not await store.hold_exclusively(identity):
            msg = "Another MindRoom runtime is already using this event journal"
            raise PermanentStartupError(msg)
        self._journal_held = True
        # Every bot follows the paused replies of its principal from these on.
        await store.load_approval_signals()

    async def event_journal_still_usable(self) -> bool:
        """Return whether this runtime still holds the journal it claimed and can still use it; true before it claims one."""
        journal = self._open_journal
        return journal is None or not self._journal_held or await journal.store.still_usable()

    def embedded_runtime(self) -> EmbeddedRuntime:
        """Return what this runtime lends the dashboard API it serves."""
        return EmbeddedRuntime(
            knowledge_refresh_scheduler=self.knowledge_refresh_scheduler,
            agent_reply_memberships=self.agent_reply_memberships,
            response_admission_gate=self.response_admission_gate,
            budget_monitor=self.budgets,
            scheduled_tasks=self.scheduled_tasks,
            thread_export_runner=self._thread_export_runner,
            leave_matrix_room=self.leave_matrix_room,
            active_calls=self.active_call_identities,
            active_script_runs=self._script_runtime.active_runs,
            config_reload_status=lambda: self.config_reload.status,
            agent_cli_registry=self.agent_cli_registry,
            script_broker=self._script_runtime.broker,
            script_worker_keepalive=self._script_runtime.touch_live_workers,
        )

    def event_journal_store(self) -> EventJournalStore:
        """Return the one journal store every bot in this process borrows.

        One database holds every principal, so opening it per bot bought
        nothing and cost a connection pool each: on PostgreSQL that multiplies
        connections by the number of configured entities until the server
        refuses them, and on SQLite it moves write contention off an in-process
        queue and onto the file lock. Bots borrow this and do not close it.
        """
        if self._open_journal is None:
            self._open_journal = open_event_journal(
                self.config.event_journal,
                runtime_paths=self.runtime_paths,
                storage_path=self.runtime_paths.storage_root,
            )
        return self._open_journal.store

    async def stop(self) -> None:  # noqa: C901, PLR0915 - one ordered shutdown sequence
        """Stop all agent bots."""
        # A reply this shutdown cancels stores its usage without reporting its cost, so budgets keep spend only when idle.
        self._budgets.keep_on_stop = await self._no_live_work()
        self.running = False
        self._runtime_ready_event.clear()
        # First, so integration events that arrive from now on are refused instead of reaching stopping bots.
        integration_runtimes = self._unbind_integrations()
        self.fleet.begin_process_shutdown()
        self.agent_cli_registry.close()
        # The runtime's own hooks end now; each bot's end with its stop, after its agent:stopped hooks ran.
        self._hook_lifetime.end()
        if self._runtime_shutdown_event is not None:
            self._runtime_shutdown_event.set()
        self._external_trigger_runtime.unbind()
        self._computer_runtime.unbind()
        # Runners borrow bots' clients, so they end before any bot stops.
        await run_shutdown_step("scheduled_tasks", self.scheduled_tasks.stop())
        try:
            await run_shutdown_step("script_runtime", self._script_runtime.shutdown())
        except Exception:
            logger.exception("Background script runtime shutdown failed")
        try:
            await run_shutdown_step("tool_job_execution", self._tool_job_runtime.quiesce())
        except Exception:
            logger.exception("Background tool job runtime shutdown failed")
        await run_shutdown_step("approval_runtime", shutdown_approval_runtime())
        await run_shutdown_step("config_reload", self.config_reload.cancel())
        owner = self._mcp_catalog_change_task_owner
        await run_shutdown_step(
            "mcp_catalog_background",
            wait_for_background_tasks(5.0, owner=owner, shutdown_intent=ORDERLY_SHUTDOWN),
        )
        await run_shutdown_step(
            "dispatch_recovery_background",
            wait_for_background_tasks(
                5.0,
                owner=self.turn_recovery.task_owner,
                shutdown_intent=ORDERLY_SHUTDOWN,
            ),
        )
        for name, runtime in integration_runtimes:
            # Acknowledged deliveries still retrying after this are cancelled: the documented accepted loss.
            await run_shutdown_step(
                f"{name}_events",
                wait_for_background_tasks(5.0, owner=runtime, shutdown_intent=ORDERLY_SHUTDOWN),
            )
        await run_shutdown_step("startup_maintenance", self._startup_maintenance.cancel())
        for service in reversed(self._services):
            await run_shutdown_step(service.name, service.stop())
        await run_shutdown_step("skill_reviews", self._skill_reviews.stop())
        await run_shutdown_step("knowledge_refresh", self._knowledge_refresh_scheduler.shutdown())
        await run_shutdown_step("bot_start_tasks", self.fleet.cancel_retries())
        await run_shutdown_step("mcp_manager", self._stop_mcp_manager())

        fleet_shutdown = await self.fleet.stop_all()
        await run_shutdown_step("attachment_cleanup", wait_for_attachment_cleanup_tasks())
        # Last, because every bot borrows it: closing it earlier would pull the
        # store out from under a bot still draining its outbox.
        journal_failures: list[BaseException] = []
        if not fleet_shutdown.owns_resources:
            try:
                await run_shutdown_step("tool_job_runtime", self._tool_job_runtime.stop())
            except Exception:
                logger.exception("Background tool job runtime shutdown failed")
            if self._open_journal is not None:
                journal, self._open_journal = self._open_journal, None
                self._journal_held = False
                close_results, cancellation = await run_shutdown_step(
                    "event_journal",
                    gather_shutdown_phase(journal.close()),
                )
                if cancellation is not None:
                    journal_failures.append(cancellation)
                journal_failures.extend(result for result in close_results if isinstance(result, BaseException))
        failures = [*fleet_shutdown.failures, *journal_failures]
        if failures:
            raise failures[0]
        self.agent_reply_membership_sync.invalidate(self.config, reason="shutdown")
        logger.info("All agent bots stopped")


async def _watch_config_task(config_path: Path, orchestrator: _MultiAgentOrchestrator) -> None:
    """Watch the config file plus every file it pulls in via !include."""

    def config_source_paths() -> Iterable[Path]:
        return {config_path, *orchestrator.config_reload.watched_source_files}

    async def on_config_change() -> None:
        logger.info("Configuration file changed; queueing hot reload")
        orchestrator.config_reload.request_reload()

    await file_watcher.watch_paths(config_source_paths, on_config_change)


async def _watch_skills_task(orchestrator: _MultiAgentOrchestrator) -> None:
    """Watch skill roots for changes and clear cached skills."""
    while not orchestrator.running:  # noqa: ASYNC110
        await asyncio.sleep(0.1)

    def skill_snapshot() -> object:
        plugins = config_plugins(orchestrator.config, orchestrator.runtime_paths)
        return get_skill_snapshot(default_skill_roots(plugins.skill_roots))

    last_snapshot = await asyncio.to_thread(skill_snapshot)
    # Runs until cancelled: a supervised loop that returned would be restarted.
    while True:
        await asyncio.sleep(1.0)
        snapshot = await asyncio.to_thread(skill_snapshot)
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            clear_skill_cache()
            logger.info("Skills changed; cache cleared")


async def _run_api_server(
    host: str,
    port: int,
    log_level: str,
    orchestrator: _MultiAgentOrchestrator,
    shutdown_requested: asyncio.Event,
) -> None:
    """Serve the bundled dashboard/API, lending it the runtime's shared state for exactly as long as it serves."""
    from mindroom.api import main as api_main  # noqa: PLC0415
    from mindroom.api.script_gateway import serve_script_gateway_listener  # noqa: PLC0415
    from mindroom.egress_broker.service import serve_egress_broker  # noqa: PLC0415

    runtime_paths = orchestrator.runtime_paths
    script_runtime = orchestrator.script_runtime
    api_server = _EmbeddedApiServerContext(host=host, port=port)
    api_main.bind_embedded_runtime(api_main.app, orchestrator.embedded_runtime())
    orchestrator.openai_responses = api_main.config_lifecycle.app_state(api_main.app).openai_responses
    config = uvicorn.Config(
        api_main.app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        ws="websockets-sansio",
    )

    async def on_started(bound_host: str, bound_port: int) -> None:
        gateway_url = await optional_script_gateway_url(runtime_paths, host=bound_host, port=bound_port)
        script_runtime.bind_api(gateway_url)

    server = _SignalAwareUvicornServer(config, shutdown_requested, on_started=on_started)
    logger.info("embedded_api_server_starting", **api_server.log_context())
    try:
        try:
            async with (
                # The broker reads the API's committed config, so service edits apply without a restart.
                serve_egress_broker(
                    runtime_paths,
                    config_provider=lambda: api_main.config_lifecycle.read_app_committed_runtime_config(
                        api_main.app,
                    )[0],
                ),
                serve_script_gateway_listener(
                    runtime_paths,
                    host=host,
                    broker=script_runtime.broker,
                    log_level=log_level,
                    agent_cli_registry=orchestrator.agent_cli_registry,
                ),
            ):
                await server.serve()
        except SystemExit as exc:
            _raise_embedded_api_server_exit(api_server, reason="server.serve() raised SystemExit", cause=exc)
    finally:
        # The script gateway's address first, so no script call reaches a broker the API no longer exposes.
        await script_runtime.unbind_api()
        api_main.unbind_embedded_runtime(api_main.app)
        clear_api_server_address()
    shutdown_expected = shutdown_requested.is_set()
    logger.info(
        "embedded_api_server_serve_returned",
        **api_server.log_context(),
        shutdown_expected=shutdown_expected,
        server_should_exit=server.should_exit,
        server_force_exit=server.force_exit,
        received_signal=server.received_signal_name,
    )
    if not shutdown_expected:
        _raise_embedded_api_server_exit(
            api_server,
            reason="server.serve() returned while application shutdown was not requested",
        )


async def _run_auxiliary_task_forever(task_name: str, operation: Callable[[], Awaitable[None]]) -> None:
    """Run one non-critical background task, restarting it whenever it exits or crashes, until cancelled."""
    loop = SupervisedLoop(task_name, operation)
    loop.start()
    try:
        await asyncio.Event().wait()
    finally:
        await loop.stop()


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
    logger.info(
        "runtime_completion_detected",
        completed_tasks=sorted(task.get_name() for task in done),
        orchestrator_done=orchestrator_task in done,
        api_done=api_task in done if api_task is not None else False,
        shutdown_requested_done=shutdown_wait_task in done,
        shutdown_requested=shutdown_requested.is_set(),
    )
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
            task_name = task.get_name()
            if api_task is not None and task is api_task:
                await _await_api_task_completion(
                    api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=api_server,
                )
                logger.info(
                    "runtime_task_completed",
                    task_name=task_name,
                    shutdown_requested=shutdown_requested.is_set(),
                )
                continue
            await task
            if task is orchestrator_task:
                if shutdown_requested.is_set():
                    logger.info(
                        "orchestrator_task_completed_after_shutdown_request",
                        task_name=task_name,
                    )
                    continue
                _raise_orchestrator_exit(
                    reason="Orchestrator task finished while application shutdown was not requested",
                )
            logger.info(
                "runtime_task_completed",
                task_name=task_name,
                shutdown_requested=shutdown_requested.is_set(),
            )
            continue
        except asyncio.CancelledError:
            logger.info("runtime_task_cancelled", task_name=task.get_name())
            continue
        except Exception as exc:
            logger.exception("runtime_task_failed", task_name=task.get_name())
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


async def _finish_runtime_shutdown(
    *,
    shutdown_wait_task: asyncio.Task[bool] | None,
    api_task: asyncio.Task[None] | None,
    orchestrator_task: asyncio.Task[None] | None,
    auxiliary_tasks: list[asyncio.Task],
    orchestrator: _MultiAgentOrchestrator | None,
    stall_detector: EventLoopStallDetector | None,
) -> None:
    """Finish one runtime cleanup sequence without duplicating partial teardown."""
    await _cancel_task_if_pending(shutdown_wait_task)
    await _cancel_task_if_pending(api_task)
    try:
        try:
            await _cancel_task_if_pending(orchestrator_task)
        finally:
            if orchestrator is not None:
                await orchestrator.stop()
    finally:
        for task in auxiliary_tasks:
            task.cancel()
        # Auxiliary tasks are non-critical and log their own failures, so none may skip the remaining cleanup
        # or replace the error that ended the runtime.
        await asyncio.gather(*auxiliary_tasks, return_exceptions=True)
        if stall_detector is not None:
            stall_detector.stop()
        reset_matrix_sync_health()
        reset_runtime_state()
        shutdown_primary_worker_manager()
        await aclose_model_http_client()


async def _wait_for_runtime_shutdown_cleanup(
    cleanup_task: asyncio.Task[None],
    *,
    shutdown_was_requested: bool,
) -> None:
    """Wait for cleanup while preserving caller cancellation semantics."""
    while True:
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            if cleanup_task.done():
                await cleanup_task
                return
            if not shutdown_was_requested:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Runtime cleanup failed while propagating caller cancellation")
                raise
            # Once orderly shutdown starts, later cancellation is duplicate
            # shutdown pressure; cleanup must finish before main returns.
            logger.info("Ignoring repeated signal cancellation during runtime cleanup")
        else:
            return


async def _watch_event_journal(
    orchestrator: _MultiAgentOrchestrator,
    shutdown_requested: asyncio.Event,
    journal_lost: asyncio.Event,
) -> None:
    """Stop the runtime once it lost its event journal's lock session or one of the journal's connections.

    Another runtime could take over a journal whose lock lapsed, and a lost connection never reconnects.
    """
    while not shutdown_requested.is_set():
        await asyncio.sleep(_EVENT_JOURNAL_CHECK_SECONDS)
        if not await orchestrator.event_journal_still_usable():
            logger.error("event_journal_lost")
            journal_lost.set()
            shutdown_requested.set()
            return


def _start_auxiliary_tasks(
    orchestrator: _MultiAgentOrchestrator,
    runtime_paths: RuntimePaths,
    shutdown_requested: asyncio.Event,
    journal_lost: asyncio.Event,
) -> list[asyncio.Task]:
    """Start the non-critical background tasks that run beside the orchestrator."""
    # First, so an invalid heap task interval fails before any task exists that shutdown could not cancel.
    heap_tasks = start_heap_tasks(runtime_paths, runtime_ready=orchestrator.wait_until_runtime_ready)
    auxiliary_specs = [
        (
            "config watcher",
            lambda: _watch_config_task(orchestrator.runtime_paths.config_path, orchestrator),
            "config_watcher_supervisor",
        ),
        ("plugins watcher", lambda: watch_plugins_task(orchestrator), "plugins_watcher_supervisor"),
        ("skills watcher", lambda: _watch_skills_task(orchestrator), "skills_watcher_supervisor"),
    ]
    tasks = [
        asyncio.create_task(
            _run_auxiliary_task_forever(task_name, operation),
            name=supervisor_name,
        )
        for task_name, operation, supervisor_name in auxiliary_specs
    ]
    tasks.append(
        create_background_task(
            _watch_event_journal(orchestrator, shutdown_requested, journal_lost),
            name="event_journal_watch",
        ),
    )
    # The heartbeat ends by itself for unpaired or rejected installs, so it must not be restarted;
    # create_background_task logs an unexpected failure as soon as it happens.
    tasks.append(create_background_task(run_provisioning_heartbeat(runtime_paths), name="provisioning_heartbeat"))
    tasks.extend(heap_tasks)
    return tasks


async def main(
    log_level: str,
    runtime_paths: RuntimePaths,
    *,
    api: bool = True,
    api_port: int = 8765,
    api_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Main entry point for the multi-agent bot system; one runtime per storage root.

    Two runtimes on one storage root would both answer every message and take
    each other's replies over, so the second refuses to start.
    """
    runtime_lock = try_exclusive_file_lock(tracking_dir(runtime_paths) / "runtime.lock")
    if runtime_lock is None:
        msg = f"Another MindRoom is already running with the storage at {runtime_paths.storage_root}"
        raise PermanentStartupError(msg)
    try:
        await _run_runtime(log_level, runtime_paths, api=api, api_port=api_port, api_host=api_host)
    finally:
        release_file_lock(runtime_lock)


async def _run_runtime(  # noqa: PLR0915
    log_level: str,
    runtime_paths: RuntimePaths,
    *,
    api: bool,
    api_port: int,
    api_host: str,
) -> None:
    """Run the multi-agent bot system until it stops."""
    orchestrator: _MultiAgentOrchestrator | None = None
    auxiliary_tasks: list[asyncio.Task] = []
    shutdown_requested = asyncio.Event()
    journal_lost = asyncio.Event()
    api_server = _EmbeddedApiServerContext(host=api_host, port=api_port)
    orchestrator_task: asyncio.Task[None] | None = None
    shutdown_wait_task: asyncio.Task[bool] | None = None
    api_task: asyncio.Task[None] | None = None
    stall_detector: EventLoopStallDetector | None = None
    # Holds the binding of the applied config as the one source dedicated workers launch from.
    worker_launch_binding = ExitStack()

    try:
        # Drop any stale worker manager before startup work builds the active runtime.
        reset_primary_worker_manager()

        # Configure logging before any background tasks or account setup begin.
        setup_logging(level=log_level, runtime_paths=runtime_paths)

        shorten_gil_switch_interval()
        stall_detector = start_event_loop_stall_detector(runtime_paths)

        # The first start after an upgrade converts every conversation database before the API binds, which can take
        # minutes, so probes meanwhile see a live process that is not ready yet instead of a closed port.
        with ExitStack() as probes:
            await prepare_runtime_storage(
                runtime_paths,
                before_conversion=(
                    partial(
                        probes.enter_context,
                        serve_startup_probes(api_host, api_port, detail="Converting conversation storage"),
                    )
                    if api
                    else None
                ),
            )
            # Before the API serves and before the homeserver answers, so the dashboard has this one load meanwhile.
            startup_config = await load_startup_config(runtime_paths, api_enabled=api)
            await migrate_team_memory(runtime_paths, startup_config)

        logger.info("Starting orchestrator...")
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=startup_config, api_enabled=api)
        # Before the config watcher starts, so it covers every included file while startup waits.
        orchestrator.config_reload.record_load_sources(startup_config.source_files, loaded=True)
        config_reload = orchestrator.config_reload
        # Bound before the API and the bots start, and kept until the worker manager has shut down; a reload that
        # is still applying, or failed partway, never reaches a worker launch. Until startup records its config as
        # applied, workers launch from the config it loaded, which no older config precedes.
        worker_launch_binding.enter_context(
            bind_worker_launch_config_source(lambda: config_reload.fully_applied_config or startup_config),
        )
        set_runtime_starting()
        auxiliary_tasks.extend(
            _start_auxiliary_tasks(orchestrator, runtime_paths, shutdown_requested, journal_lost),
        )

        if api:
            api_task = asyncio.create_task(
                _run_api_server(api_host, api_port, log_level, orchestrator, shutdown_requested),
                name="api_server",
            )
        elif egress_broker_enabled(runtime_paths):
            logger.warning(
                "egress_broker_not_started",
                reason="the egress broker runs with the embedded API server, which is disabled",
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

    except asyncio.CancelledError:
        if not shutdown_requested.is_set():
            raise
        # After an explicit shutdown request, cancellation is shutdown pressure
        # rather than caller cancellation and must not bypass orderly cleanup.
        logger.info("Ignoring duplicate signal cancellation during requested shutdown")
    except KeyboardInterrupt:
        shutdown_requested.set()
        logger.info("Multi-agent bot system stopped by user")
    except PermanentStartupError as exc:
        shutdown_requested.set()
        logger.error("MindRoom startup failed", error=str(exc))  # noqa: TRY400
        raise
    except Exception:
        shutdown_requested.set()
        logger.exception("Error in MindRoom runtime")
        raise
    finally:
        shutdown_was_requested = shutdown_requested.is_set()
        shutdown_requested.set()
        cleanup_task = asyncio.create_task(
            _finish_runtime_shutdown(
                shutdown_wait_task=shutdown_wait_task,
                api_task=api_task,
                orchestrator_task=orchestrator_task,
                auxiliary_tasks=auxiliary_tasks,
                orchestrator=orchestrator,
                stall_detector=stall_detector,
            ),
            name="runtime_shutdown",
        )
        try:
            await _wait_for_runtime_shutdown_cleanup(
                cleanup_task,
                shutdown_was_requested=shutdown_was_requested,
            )
        finally:
            worker_launch_binding.close()
    if journal_lost.is_set():
        # Exiting with a failure lets a supervisor that restarts only on failure bring the runtime back.
        msg = "MindRoom stopped because it lost its event journal's lock session or one of its database connections"
        raise EventJournalLostError(msg)
