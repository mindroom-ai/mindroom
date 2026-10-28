"""Multi-agent orchestration runtime."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext, suppress
from contextvars import Context
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, NoReturn, cast, overload
from uuid import uuid4

import uvicorn

from mindroom import constants
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.agents import ensure_default_agent_workspaces
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
    run_blocking_until_complete,
    wait_for_background_tasks,
)
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.constants import ROUTER_AGENT_NAME, ROUTER_DISPLAY_NAME, tracking_dir
from mindroom.credentials.legacy_tool_credentials import migrate_tool_credential_defaults
from mindroom.delegation.recovery import cancel_approval_delegations
from mindroom.desktop.identity import controller_identity_for_live_bot
from mindroom.egress_broker.env import egress_broker_enabled
from mindroom.embeddings.embedder_health import check_embedder_health, handle_embedder_config_reload
from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)
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
    HookRegistry,
    HookRegistryState,
    build_hook_matrix_admin,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
)
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
from mindroom.knowledge.status import reconcile_knowledge_mode_transition_states
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.knowledge_graph.runtime import KnowledgeGraphRuntime
from mindroom.legacy_state_root_records import migrate_state_root_records
from mindroom.matrix.health import reset_matrix_sync_health
from mindroom.matrix.provisioning_heartbeat import run_provisioning_heartbeat
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
    ManagedAccountProvisioningRequest,
    create_agent_user,
    load_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.matrix_rtc.call_manager import preload_matrix_call_dependencies
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import mcp_tool_name
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import MemoryAutoFlushService
from mindroom.private_storage.legacy_private_storage import migrate_private_storage
from mindroom.providers.model_http_client import aclose_model_http_client
from mindroom.response_activity import ResponseIdentity
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
from mindroom.scheduling.executor import set_scheduling_hook_registry
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.skill_learning.runner import SkillReviewRunner
from mindroom.startup_errors import EventJournalHoldLostError, PermanentStartupError
from mindroom.startup_maintenance import StartupMaintenanceController
from mindroom.tool_system.plugins import (
    PluginReloadResult,
    apply_prepared_plugin_reload,
    deactivate_plugins,
    load_plugins,
    prepare_plugin_reload,
    reload_plugins,
)
from mindroom.tool_system.skills import clear_skill_cache, get_skill_snapshot
from mindroom.workers.backends.legacy_state_root_mounts import retire_state_root_worker_mounts
from mindroom.workers.runtime import (
    clear_worker_validation_snapshot_cache,
    lease_configured_primary_worker_manager,
    reset_primary_worker_manager,
    shutdown_primary_worker_manager,
)

from . import file_watcher
from .bot import AgentBot, TeamBot, create_bot_for_entity
from .credentials.sync import sync_env_to_credentials
from .event_journal.opening import OpenEventJournal, bind_event_journal, open_event_journal
from .logging_config import get_logger, setup_logging
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
from .orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from .orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from .thread_export.workspace_sync import WorkspaceThreadExportDeps, WorkspaceThreadExportRunner

if TYPE_CHECKING:
    import socket
    from collections.abc import Awaitable, Callable, Iterable, Mapping
    from pathlib import Path
    from types import FrameType

    import nio

    from mindroom.config.reload_status import ConfigReloadStatus
    from mindroom.desktop.identity import DesktopControllerIdentity
    from mindroom.event_journal import ApprovalContinuation, ApprovalDeliveryView
    from mindroom.hooks import HookMatrixAdmin, HookMessageSender, HookRoomStatePutter, HookRoomStateQuerier

    from .config.main import Config
    from .constants import RuntimePaths
    from .event_journal import EventJournalStore
    from .orchestration.config_updates import ConfigUpdatePlan
    from .orchestration.services import RuntimeService
logger = get_logger(__name__)


_EMBEDDED_API_SHUTDOWN_GRACE_SECONDS = 5.0
_EVENT_JOURNAL_HOLD_CHECK_SECONDS = 30.0


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
        shutdown_requested: asyncio.Event | None,
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
        if self._shutdown_requested is not None:
            self._shutdown_requested.set()
        if self.should_exit and signal_number == int(signal.SIGINT):
            self.force_exit = True
            return
        self.should_exit = True


@dataclass
class _MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    runtime_paths: RuntimePaths
    api_enabled: bool = True
    storage_path: Path = field(init=False)
    config_path: Path = field(init=False)
    fleet: EntityFleet = field(init=False)
    # The one journal every bot in this process borrows a store from, opened on
    # first use and closed after the last bot stops.
    _open_journal: OpenEventJournal | None = field(default=None, init=False, repr=False)
    _journal_held: bool = field(default=False, init=False, repr=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
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
    _dispatch_recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _dispatch_recovery_task_owner: object = field(default_factory=object, init=False, repr=False)
    _dispatch_recovery_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _dispatch_recovery_requested: bool = field(default=False, init=False, repr=False)
    _response_admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate, init=False)
    _mcp_catalog_change_task_owner: object = field(default_factory=object, init=False, repr=False)
    _pending_mcp_catalog_restarts: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    plugin_watch: PluginWatchState = field(init=False)
    agent_cli_registry: TurnToolRegistry = field(default_factory=TurnToolRegistry, init=False)
    _knowledge_refresh_scheduler: KnowledgeRefreshScheduler = field(init=False)
    _knowledge_source_watcher: KnowledgeSourceWatcher = field(init=False)
    _hook_registry_state: HookRegistryState = field(
        default_factory=lambda: HookRegistryState(HookRegistry.empty()),
        init=False,
        repr=False,
    )
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
        """Store canonical derived paths from the explicit runtime context."""
        self.storage_path = self.runtime_paths.storage_root
        self.config_path = self.runtime_paths.config_path
        self.fleet = EntityFleet(
            EntityFleetHooks(
                create_bots=self._create_bots,
                start_blocked=self._entities_blocked_by_failed_mcp_servers,
                entities_changing=self._entities_changing,
                entities_started=self._entities_started,
                setup_rooms_after_retry=self._setup_rooms_after_retry,
                reconcile_removed=lambda names: self._approval_recovery.reconcile_removed_entities(names),
                end_replies=self._end_entity_replies,
            ),
        )
        self._knowledge_refresh_scheduler = KnowledgeRefreshScheduler.from_runtime(self.runtime_paths)
        self._knowledge_source_watcher = KnowledgeSourceWatcher(self._knowledge_refresh_scheduler, self.runtime_paths)
        self.agent_reply_membership_sync = AgentReplyMembershipSync(self.agent_reply_memberships)
        self.plugin_watch = PluginWatchState(runtime_paths=self.runtime_paths)
        self._computer_runtime = ComputerRuntimeCoordinator(
            runtime_paths=self.runtime_paths,
            api_enabled=self.api_enabled,
            agent_reply_memberships=self.agent_reply_memberships,
        )
        self._external_trigger_runtime = ExternalTriggerRuntimeCoordinator(
            api_enabled=self.api_enabled,
            agent_reply_memberships=self.agent_reply_memberships,
        )
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
            journal_provider=self._shared_journal_store,
        )
        self._thread_export_runner = WorkspaceThreadExportRunner(
            WorkspaceThreadExportDeps(
                runtime_paths=self.runtime_paths,
                config_provider=lambda: self.config,
                bot_provider=lambda entity_name: self.agent_bots.get(entity_name),
                response_admission_gate=self._response_admission_gate,
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
                runtime_config=self.config,
                required_backend_locator=required_backend_locator,
            ),
            api_enabled=self.api_enabled,
        )
        self.config_reload = ConfigReloadLifecycle(
            runtime_paths=self.runtime_paths,
            is_running=lambda: self.running,
            current_config=self._require_config,
            agent_bots=lambda: self.agent_bots,
            load_config=partial(load_published_config, self.runtime_paths, api_enabled=self.api_enabled),
            apply_update_plan=self._apply_config_update_plan,
            response_admission_gate=self._response_admission_gate,
            before_runtime_replacement=self._thread_export_runner.prepare_runtime_replacement,
            rebind_published_config_consumers=lambda: self._bind_trigger_runtimes(),
            config_update_lock=self._config_update_lock,
        )
        self._approval_transport = ApprovalMatrixTransport(
            bot_provider=lambda agent_name: self.agent_bots.get(agent_name),
        )
        self._approval_recovery = ApprovalRecovery(
            deliver_unavailable_notice=self._approval_transport.deliver_unavailable_notice,
            journal_provider=self._shared_journal_store,
            entity_configured=lambda name: (
                self.config is not None and (name in self.config.agents or name in self.config.teams)
            ),
            entity_permanently_unavailable=self.fleet.is_disabled,
            recover_unavailable_final=self._recover_unavailable_final,
            cancel_delegations=lambda continuation, reason: cancel_approval_delegations(
                continuation,
                config=self._require_config(),
                runtime_paths=self.runtime_paths,
                reason=reason,
            ),
        )
        self._rooms = RoomSetup(
            runtime_paths=self.runtime_paths,
            config_provider=self._require_config,
            router_bot=self._router_bot,
            refresh_agent_reply_memberships=lambda: self.refresh_agent_reply_memberships(),
        )
        self._startup_maintenance = StartupMaintenanceController(
            setup_rooms_and_memberships=self._setup_startup_rooms_and_memberships,
            sync_runtime_support=lambda config: self._sync_runtime_support_services(config),
            mark_runtime_support_ready=lambda: self._approval_recovery.mark_startup_runtime_support_ready(),
        )

    @property
    def agent_bots(self) -> Mapping[str, AgentBot | TeamBot]:
        """Return every managed bot by entity name; the fleet owns their lifecycle."""
        return self.fleet.bots

    @property
    def hook_registry(self) -> HookRegistry:
        """Return the currently active hook registry."""
        return self._hook_registry_state.registry

    @hook_registry.setter
    def hook_registry(self, value: HookRegistry) -> None:
        """Update the active hook registry snapshot."""
        self._hook_registry_state.registry = value

    @property
    def script_runtime(self) -> ScriptRuntimeLifecycle:
        """Return the process-local background-script lifecycle collaborator."""
        return self._script_runtime

    def active_call_identities(self) -> list[ResponseIdentity]:
        """Return one identity per voice call that any bot has joined or is joining."""
        return [
            ResponseIdentity(responder=entity_name, requester_id=requester_id)
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
        if bot is not None and bot.approval_store.principal_id != principal_id:
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
            bot = AgentBot(
                agent_user,
                self.storage_path,
                self._require_config(),
                self.runtime_paths,
                config_path=self.config_path,
                journal_store=self._shared_journal_store(),
                agent_reply_memberships=self.agent_reply_memberships,
            )
            bot.orchestrator = self
            bot.hook_registry = self.hook_registry
            self._bind_response_admission_gate(bot)
            if bot.approval_store.principal_id != principal_id:
                logger.error(
                    "approval_original_principal_identity_mismatch",
                    approval_id=continuation.approval_id,
                    expected_principal_id=principal_id,
                    actual_principal_id=bot.approval_store.principal_id,
                )
                return False

        return await bot.recover_approval_final(continuation.approval_id)

    async def leave_matrix_room(self, agent_name: str, room_id: str) -> bool:
        """Route dashboard departures through the current bot's durable membership owner."""
        if not self._response_admission_gate.admit():
            msg = "MindRoom is starting or reloading; retry the room departure"
            raise RuntimeError(msg)
        try:
            bot = self.agent_bots.get(agent_name)
            if bot is None or not bot.running or bot.client is None:
                msg = f"No running Matrix owner for {agent_name}"
                raise RuntimeError(msg)
            return await bot.change_local_membership(room_id, "leave")
        finally:
            self._response_admission_gate.release()

    def _bind_response_admission_gate(self, bot: AgentBot | TeamBot) -> None:
        """Share the orchestrator-owned response admission gate with one managed bot."""
        bot.admission_gate = self._response_admission_gate

    def _approval_cards(self) -> ApprovalDeliveryView | None:
        """Return the router principal's approval-card store, once it exists.

        Bound before the router bot is built as well as after, because the
        transport is configured on every config reload. The store belongs to
        the bot, so the early call has nothing to give; the manager treats a
        missing store as "no card is recoverable", and the rebind that follows
        bot construction supplies the real one.
        """
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        return None if router_bot is None else router_bot.approval_store

    def _bind_started_runtime_support_services(self, bots: list[AgentBot | TeamBot]) -> None:
        """Bind current runtime support objects needed by live callbacks."""
        for bot in bots:
            self._bind_response_admission_gate(bot)
        self._configure_approval_store_transport()
        self._thread_export_runner.queue_full_pass()

    async def _start_runtime_support(self, bots: list[AgentBot | TeamBot]) -> None:
        """Bind live-callback support, then start the job and script runtimes that deliver through those bots."""
        self._bind_started_runtime_support_services(bots)
        await self._tool_job_runtime.sync()
        await self._script_runtime.start()

    async def _setup_startup_rooms_and_memberships(self, bots: list[AgentBot | TeamBot]) -> None:
        """Run startup room setup, then publish trigger delivery runtime."""
        await run_with_retry(
            "Setting up Matrix rooms and memberships",
            lambda: self._rooms.setup(bots),
            permanent_error_check=is_permanent_startup_error,
            update_runtime_state=False,
        )
        self._bind_trigger_runtimes()

    def _configure_approval_store_transport(self) -> None:
        """Bind approval transport hooks to the current shared runtime services."""
        transport = self._approval_transport
        initialize_approval_store(
            self.runtime_paths,
            prepare_event=transport.prepare_approval_event,
            send_delivery=transport.send_approval_delivery,
            resolve_delivery=transport.resolve_approval_delivery,
            resolve_action_delivery=transport.resolve_approval_action_delivery,
            cards=self._approval_cards(),
            transport_sender=transport.transport_sender_id,
            sending_device=transport.transport_device_id,
            continuation_ready=transport.wake_continuation_sources,
            recovery=self._approval_recovery,
            responder_principal=transport.responder_principal_id,
            router_missing=transport.router_missing,
        )

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

    def _running_startup_maintenance_bots(self) -> list[AgentBot | TeamBot]:
        """Return currently running bots in startup-maintenance order."""
        router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
        bots: list[AgentBot | TeamBot] = []
        if router_bot is not None and router_bot.running:
            bots.append(router_bot)
        bots.extend(
            bot for entity_name, bot in self.agent_bots.items() if entity_name != ROUTER_AGENT_NAME and bot.running
        )
        return bots

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

    def _ready_bot_for_turn_journal_recovery(
        self,
        entity_name: str,
        config: Config,
    ) -> AgentBot | TeamBot | None:
        """Return the current bot when its fleet dependencies are ready."""
        bot = self.agent_bots.get(entity_name)
        required_entities = (
            [entity_name]
            if entity_name == ROUTER_AGENT_NAME
            else ([entity_name, *config.teams[entity_name].agents] if entity_name in config.teams else [entity_name])
        )
        if bot is None or any(
            self.entity_first_sync_complete(required_entity) is not True for required_entity in required_entities
        ):
            return None
        return bot

    async def _recover_current_turn_journal_bot(self, entity_name: str) -> Exception | None:
        """Recover the ready current generation for one configured entity."""
        config = self.config
        if config is None or entity_name not in configured_entity_names(config):
            return None
        bot = self._ready_bot_for_turn_journal_recovery(entity_name, config)
        if bot is None:
            return None
        try:
            await bot.recover_pending_turn_journal_events()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "turn_dispatch_recovery_failed",
                agent_name=entity_name,
            )
            return error
        return None

    async def _recover_ready_turn_journal_events(self) -> None:
        """Recover fleet-dependent turns whose required runtimes are ready."""
        async with self._dispatch_recovery_lock:
            config = self.config
            if config is None:
                return
            first_error: Exception | None = None
            ready_bots: list[tuple[str, AgentBot | TeamBot]] = []
            for entity_name in configured_entity_names(config):
                bot = self._ready_bot_for_turn_journal_recovery(entity_name, config)
                if bot is None:
                    continue
                ready_bots.append((entity_name, bot))
            for _entity_name, bot in ready_bots:
                bot.release_pending_turn_journal_replay()
            for entity_name, _released_bot in ready_bots:
                error = await self._recover_current_turn_journal_bot(entity_name)
                first_error = first_error or error
            if first_error is not None:
                raise first_error

    def _schedule_ready_turn_dispatch_recovery(self) -> None:
        """Coalesce bot-ready signals into one orchestrator-owned recovery task."""
        if not self._runtime_ready_event.is_set():
            return
        config = self.config
        if config is not None:
            for entity_name in configured_entity_names(config):
                ready_bot = self._ready_bot_for_turn_journal_recovery(entity_name, config)
                if ready_bot is not None:
                    ready_bot.release_pending_turn_journal_replay()
        self._dispatch_recovery_requested = True
        task = self._dispatch_recovery_task
        if task is not None and not task.done():
            return
        self._dispatch_recovery_task = create_background_task(
            self._run_scheduled_turn_dispatch_recovery(),
            name="recover_ready_turn_journal_events",
            owner=self._dispatch_recovery_task_owner,
        )

    async def _run_scheduled_turn_dispatch_recovery(self) -> None:
        """Drain readiness signals without blocking a Matrix sync callback."""

        async def recover() -> None:
            if not self._runtime_ready_event.is_set():
                return
            await self._recover_ready_turn_journal_events()

        current_task = asyncio.current_task()
        try:
            while self._dispatch_recovery_requested:
                self._dispatch_recovery_requested = False
                await run_with_retry(
                    "Recovering ready turn dispatch obligations",
                    recover,
                    update_runtime_state=False,
                )
        finally:
            if self._dispatch_recovery_task is current_task:
                self._dispatch_recovery_task = None

    async def _sync_runtime_support_services(self, config: Config, *, previous_config: Config | None = None) -> None:
        """Bring every runtime service in line with the active config."""
        if previous_config is not None:
            await asyncio.to_thread(
                reconcile_knowledge_mode_transition_states,
                previous_config,
                config,
                self.runtime_paths,
            )
        ensure_default_agent_workspaces(config, self.storage_path)
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
        config = self.config
        if manager is None or config is None:
            return set()
        failed_server_ids = manager.failed_required_server_ids()
        if not failed_server_ids:
            return set()
        return mcp_dependents(config, failed_server_ids) & entity_names

    def _log_mcp_degraded_entities(self, config: Config) -> None:
        """Warn once per unavailable optional MCP server about entities running without its tools."""
        manager = self._mcp_manager
        if manager is None:
            return
        running_entities = {entity_name for entity_name, bot in self.agent_bots.items() if bot.running}
        for server_id in sorted(manager.failed_server_ids() - manager.failed_required_server_ids()):
            degraded_entities = config.get_entities_referencing_tools({mcp_tool_name(server_id)}) & running_entities
            if not degraded_entities:
                continue
            logger.warning(
                "Entities running without tools from unavailable MCP server",
                server_id=server_id,
                degraded_entities=sorted(degraded_entities),
            )

    @staticmethod
    def _entity_display_name(config: Config, entity_name: str) -> str:
        """Return the Matrix display name for one configured managed entity."""
        if entity_name == ROUTER_AGENT_NAME:
            return ROUTER_DISPLAY_NAME
        if entity_name in config.agents:
            return config.agents[entity_name].display_name
        if entity_name in config.teams:
            return config.teams[entity_name].display_name
        return entity_name

    @staticmethod
    def _entity_requested_username(config: Config, entity_name: str) -> str | None:
        """Return a generated agent's stored Matrix username, so a wiped matrix_state recreates the same ID."""
        instance = config.personal_agent_instance(entity_name)
        return None if instance is None else instance.matrix_username

    async def _prepare_entity_accounts(
        self,
        config: Config,
        entity_names: Iterable[str],
    ) -> dict[str, AgentMatrixUser]:
        """Ensure managed Matrix accounts exist before runtime bot construction."""
        homeserver = constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths)
        users: dict[str, AgentMatrixUser] = {}
        entity_names = tuple(entity_names)
        self._preflight_account_provisioning(config, entity_names=entity_names, include_internal_user=False)

        async def _prepare_accounts() -> None:
            nonlocal users
            prepared_users: dict[str, AgentMatrixUser] = {}
            for entity_name in entity_names:
                prepared_users[entity_name] = await create_agent_user(
                    homeserver,
                    entity_name,
                    self._entity_display_name(config, entity_name),
                    runtime_paths=self.runtime_paths,
                    username=self._entity_requested_username(config, entity_name),
                )
            self._validate_entity_accounts(config)
            users = prepared_users

        await run_with_retry(
            "Preparing managed Matrix accounts",
            _prepare_accounts,
            permanent_error_check=is_permanent_startup_error,
            update_runtime_state=not self.running,
        )
        return users

    def _validate_entity_accounts(self, config: Config) -> None:
        """Validate persisted Matrix identities for all configured runtime entities."""
        try:
            entity_identity_registry(config, self.runtime_paths)
        except (DuplicateManagedEntityIdentityError, MissingManagedEntityAccountError) as exc:
            raise PermanentStartupError(str(exc)) from exc

    def _preflight_account_provisioning(
        self,
        config: Config,
        *,
        entity_names: Iterable[str],
        include_internal_user: bool,
    ) -> None:
        """Reject account localpart collisions before any missing account is created."""
        requests: list[ManagedAccountProvisioningRequest] = []
        if include_internal_user and config.mindroom_user is not None:
            requests.append(
                ManagedAccountProvisioningRequest(
                    INTERNAL_USER_AGENT_NAME,
                    username=config.mindroom_user.username,
                ),
            )
        requests.extend(
            ManagedAccountProvisioningRequest(
                entity_name,
                username=self._entity_requested_username(config, entity_name),
            )
            for entity_name in entity_names
        )
        preflight_managed_account_provisioning(requests, self.runtime_paths)

    def validate_managed_entity_identities(self) -> None:
        """Validate persisted managed Matrix identities for the live config."""
        self._validate_entity_accounts(self._require_config())

    def _create_managed_bot(
        self,
        entity_name: str,
        config: Config,
        agent_user: AgentMatrixUser,
    ) -> AgentBot | TeamBot:
        """Build one runtime-managed bot; the fleet registers it."""
        bot = cast(
            "AgentBot | TeamBot",
            create_bot_for_entity(
                entity_name,
                agent_user,
                config,
                self.runtime_paths,
                self.storage_path,
                config_path=self.config_path,
                journal_store=self._shared_journal_store(),
                agent_reply_memberships=self.agent_reply_memberships,
                agent_reply_membership_sync=(
                    self.agent_reply_membership_sync if entity_name == ROUTER_AGENT_NAME else None
                ),
                room_activity_observer=self._thread_export_runner.mark_room_activity,
            ),
        )
        bot.orchestrator = self
        bot.hook_registry = self.hook_registry
        self._bind_response_admission_gate(bot)
        return bot

    async def _create_bots(self, entity_names: set[str], config: Config) -> dict[str, AgentBot | TeamBot]:
        """Prepare Matrix accounts, then build bots for configured entities."""
        entity_users = await self._prepare_entity_accounts(config, sorted(entity_names))
        return {
            entity_name: self._create_managed_bot(entity_name, config, entity_users[entity_name])
            for entity_name in sorted(entity_names)
        }

    async def _entities_changing(self, entity_names: set[str]) -> None:
        """Withdraw capabilities bound to these entities before any of them stops."""
        self._external_trigger_runtime.unbind_for_entity_changes(entity_names)
        self._computer_runtime.unbind_for_entity_changes(entity_names)
        if ROUTER_AGENT_NAME in entity_names:
            # Fence room-backed grants before stopping their sole control client.
            self.invalidate_agent_reply_memberships(reason="router_replacement")

    async def _entities_started(self, results: EntityStartResults) -> None:
        """Publish one batch of start outcomes to the runtime services that depend on live bots."""
        self._bind_started_runtime_support_services(results.started_bots)
        await self._approval_recovery.reconcile_unavailable_entities(results.permanently_failed_entities)
        self._schedule_ready_turn_dispatch_recovery()

    async def _setup_rooms_after_retry(self, entity_name: str) -> None:
        """Reconcile rooms after a background retry started one entity."""
        bots_to_setup = self._bots_to_setup_after_background_start(entity_name)
        if bots_to_setup:
            await run_with_retry(
                f"Updating Matrix room memberships for {entity_name}",
                partial(self._rooms.setup, bots_to_setup),
                permanent_error_check=is_permanent_startup_error,
                update_runtime_state=False,
            )
        self._bind_trigger_runtimes()

    def _bind_trigger_runtimes(self) -> None:
        """Bind external-trigger and computer delivery once their router and targets run."""
        self._external_trigger_runtime.bind_if_ready(self.config, self.agent_bots)
        self._computer_runtime.bind_if_ready(self.config, self.agent_bots)

    async def _end_entity_replies(self, entity_names: set[str]) -> None:
        """End the journal replies of removed entities; no bot remains to finish them."""
        await self._shared_journal_store().end_entity_replies(entity_names.__contains__, now_ns=time.time_ns())

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
            config = self._require_config()
            await self._script_runtime.interrupt_for_plugin_reload()
            logger.info(
                "Reloading plugins",
                source=source,
                changed_paths=[str(path) for path in changed_paths],
            )
            watch_roots, watch_root_snapshots = self.plugin_watch.capture(config)
            try:
                try:
                    result = reload_plugins(config, self.runtime_paths)
                except Exception:
                    recovery_result, warning_message, warning_kwargs = _recover_failed_plugin_reload(
                        config,
                        self.runtime_paths,
                    )
                    self._activate_hook_registry(recovery_result.hook_registry)
                    clear_worker_validation_snapshot_cache()
                    self.plugin_watch.replace_snapshots(watch_roots, watch_root_snapshots)
                    logger.warning(warning_message, source=source, **warning_kwargs)
                    raise
                self._activate_hook_registry(result.hook_registry)
                clear_worker_validation_snapshot_cache()
                self.plugin_watch.replace_snapshots(watch_roots, watch_root_snapshots)
                logger.info(
                    "Plugin reload complete",
                    source=source,
                    active_plugins=list(result.active_plugin_names),
                    cancelled_task_count=result.cancelled_task_count,
                )
                return result
            finally:
                await self._script_runtime.complete_worker_replacement()

    def _publish_config(self, plan: ConfigUpdatePlan, commit_plugins: Callable[[], HookRegistry] | None) -> None:
        """Make the new config, its plugins, and its hook registry live in one step."""
        new_config = plan.new_config
        self.config = new_config
        if plan.reply_membership_policy_changed:
            self.invalidate_agent_reply_memberships(reason="config_reload")
        if commit_plugins is None:
            self.plugin_watch.sync_roots(new_config)
            hook_registry = self.hook_registry
        else:
            hook_registry = commit_plugins()
        self._activate_hook_registry(hook_registry)
        clear_worker_validation_snapshot_cache()

    def _prepare_plugins(self, plan: ConfigUpdatePlan) -> Callable[[], HookRegistry] | None:
        """Stage changed plugins before anything stops, so a broken plugin fails the reload with every bot intact.

        Returns the step that commits the staged plugins and their watcher baselines.
        """
        if not plan.plugin_changes:
            return None
        roots, root_snapshots = self.plugin_watch.capture(plan.new_config)
        prepared = prepare_plugin_reload(plan.new_config, self.runtime_paths, skip_broken_plugins=True)

        def commit() -> HookRegistry:
            hook_registry = apply_prepared_plugin_reload(prepared, cancel_existing_tasks=True).hook_registry
            self.plugin_watch.replace_snapshots(roots, root_snapshots)
            return hook_registry

        return commit

    async def initialize(self, config: Config) -> None:
        """Initialize all managed bots from the config startup loaded."""
        set_runtime_starting("Preparing agents")
        logger.info("Initializing multi-agent system...")

        await preload_matrix_call_dependencies(config)
        freeze_startup_heap()
        hook_registry = await asyncio.to_thread(self._build_hook_registry, config)
        entity_names = configured_entity_names(config)
        self._preflight_account_provisioning(config, entity_names=entity_names, include_internal_user=True)
        await self._prepare_user_account(config, update_runtime_state=True)
        entity_users = await self._prepare_entity_accounts(config, entity_names)
        self.config = config
        warn_about_config_risks(config, self.runtime_paths)
        self.agent_reply_memberships.invalidate(config, reason="initial_config")
        await self._bind_event_journal()
        await self._tool_job_runtime.initialize()
        self._activate_hook_registry(hook_registry)
        await self._sync_mcp_manager(config)
        self._configure_approval_store_transport()
        for entity_name in entity_names:
            self.fleet.add(entity_name, self._create_managed_bot(entity_name, config, entity_users[entity_name]))

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self, config: Config) -> None:
        """Start all agent bots from the config startup loaded and publish readiness state."""
        try:
            await self._start_runtime(config)
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
        return build_hook_matrix_admin(router_bot.client, self.runtime_paths, config=self.config)

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

    async def handle_bot_ready(self, bot: AgentBot | TeamBot) -> None:
        """Handle bot-ready notifications through the public runtime protocol."""
        if bot.agent_name == ROUTER_AGENT_NAME:
            self._router_reply_memberships_live_sync_ready.set()
        if bot.agent_name == ROUTER_AGENT_NAME and bot.running and bot.client is not None:
            await self._approval_recovery.mark_router_ready()
        if self.config is not None and self.config.personal_agent_instance(bot.agent_name) is not None:
            router = self.agent_bots.get(ROUTER_AGENT_NAME)
            if isinstance(router, AgentBot):
                router.personal_room_target_ready()
        self._schedule_ready_turn_dispatch_recovery()

    def invalidate_agent_reply_memberships(self, *, reason: str) -> None:
        """Synchronously revoke every room-backed reply grant."""
        if self.config is not None:
            revocation_wave_requested = self.agent_reply_membership_sync.invalidate(self.config, reason=reason)
            if not revocation_wave_requested:
                return
            for bot in self.agent_bots.values():
                bot.schedule_reply_authorized_call_revocation()

    async def reconcile_reply_authorized_calls(self) -> None:
        """Recheck every active MatrixRTC call against current reply access."""
        await asyncio.gather(*(bot.reconcile_reply_authorized_calls() for bot in self.agent_bots.values()))

    async def reconcile_pending_invites(self) -> None:
        """Recheck cached invites against each router, agent, or team policy."""
        await asyncio.gather(*(bot.reconcile_pending_invites() for bot in self.agent_bots.values()))

    async def revoke_reply_authorized_calls(self) -> None:
        """End active MatrixRTC calls that no longer pass current reply access."""
        await asyncio.gather(*(bot.revoke_reply_authorized_calls() for bot in self.agent_bots.values()))

    async def refresh_agent_reply_memberships(self) -> None:
        """Refresh room-backed reply grants through the router's Matrix client."""
        config = self._require_config()
        router_bot = self._router_bot()
        if router_bot is None or router_bot.client is None:
            self.invalidate_agent_reply_memberships(reason="router_unavailable")
            return
        await self.agent_reply_memberships.refresh(config, self.runtime_paths, router_bot.client)
        self.agent_reply_membership_sync.record_authoritative_refresh(config)
        self._schedule_ready_turn_dispatch_recovery()
        for bot in self.agent_bots.values():
            bot.schedule_pending_invite_reconciliation()
        await self.revoke_reply_authorized_calls()
        if self.agent_reply_memberships.needs_refresh(config):
            return
        for bot in self.agent_bots.values():
            bot.schedule_reply_authorized_call_reconciliation()

    async def _end_unconfigured_entity_replies(self, config: Config) -> None:
        """End the replies of entities removed while MindRoom was stopped; no bot finishes them."""
        configured = {ROUTER_AGENT_NAME, *config.agents, *config.teams}
        await self._shared_journal_store().end_entity_replies(
            lambda entity_name: entity_name not in configured,
            now_ns=time.time_ns(),
        )

    async def _start_runtime(self, config: Config) -> None:
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
            await self.initialize(config)
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

        config = self._require_config()
        await self._end_unconfigured_entity_replies(config)
        self._log_mcp_degraded_entities(config)
        self._rooms.resolve_aliases(started_bots, config)
        phase_started = log_startup_phase_started("bind_runtime_support")
        await self._start_runtime_support(started_bots)
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
            self._schedule_ready_turn_dispatch_recovery()
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

        assert self._response_admission_gate.closed, "startup publication must own response admission"
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

    async def _update_unchanged_bots(self, new_config: Config) -> None:
        """Give the new config to the bots this reload keeps running."""
        for bot in self.agent_bots.values():
            bot.config = new_config
            bot.enable_streaming = new_config.defaults.enable_streaming
            bot.hook_registry = self.hook_registry

        for entity_name, bot in self.agent_bots.items():
            try:
                await bot._set_presence_with_model_info()
            except Exception:
                logger.exception("bot_presence_update_failed", agent=entity_name)
            logger.debug("bot_config_updated", agent=entity_name)

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
            _hook_registry_state=self._hook_registry_state,
            changed_entities=tuple(sorted(changed_entities)),
            added_entities=tuple(sorted(added_entities)),
            removed_entities=tuple(sorted(removed_entities)),
            plugin_changes=plugin_changes,
        )
        await emit(self.hook_registry, EVENT_CONFIG_RELOADED, context)

    async def _handle_mcp_catalog_change(self, server_id: str) -> None:
        """Restart entities that reference one changed MCP catalog."""
        try:
            config = self.config
            if not self.running or config is None:
                return
            if not config.get_entities_referencing_tools({mcp_tool_name(server_id)}):
                clear_worker_validation_snapshot_cache()
                return
            await self.config_reload.apply_with_response_admission(
                partial(self._apply_mcp_catalog_change, server_id),
                operation_name="MCP catalog restart",
                request_is_current=lambda: self.running and self.config is not None,
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
            config = self.config
            if not self.running or config is None:
                return
            clear_worker_validation_snapshot_cache()
            changed_entities = catalog_change_restarts(config, {server_id}, self.fleet.names)
            if not changed_entities:
                return
            logger.info(
                "Restarting entities after MCP catalog change",
                server_id=server_id,
                entities=sorted(changed_entities),
            )
            async with self._paused_startup_maintenance():
                start_results = await self.fleet.replace(changed_entities, config)
                if start_results.started_bots:
                    await self._rooms.setup(start_results.started_bots)
                self._bind_trigger_runtimes()
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
        elif plan.matrix_space_changed or plan.room_metadata_changed:
            room_ids = await self._rooms.ensure_rooms_exist()
            if plan.room_metadata_changed:
                await self._rooms.reconcile_room_policies(room_ids)
            await self._rooms.link_root_space(room_ids)

    async def _prepare_accounts_for_config_update(self, new_config: Config, plan: ConfigUpdatePlan) -> None:
        """Prepare or validate managed Matrix accounts before publishing a reloaded config."""
        entities_requiring_account_check = set(plan.start | plan.restart)
        self._preflight_account_provisioning(
            new_config,
            entity_names=entities_requiring_account_check,
            include_internal_user=plan.mindroom_user_changed,
        )
        if plan.mindroom_user_changed:
            await self._prepare_user_account(new_config, update_runtime_state=not self.running)
        if entities_requiring_account_check:
            await self._prepare_entity_accounts(new_config, entities_requiring_account_check)
        elif plan.mindroom_user_changed:
            self._validate_entity_accounts(new_config)

    async def _finalize_config_reload(
        self,
        *,
        new_config: Config,
        current_config: Config,
        changed_entities: set[str],
        added_entities: set[str],
        removed_entities: set[str],
        plugin_changes: tuple[str, ...],
    ) -> None:
        """Publish post-reload runtime services and external trigger delivery binding."""
        handle_embedder_config_reload(current_config, new_config, self.runtime_paths)
        await self._sync_runtime_support_services(new_config, previous_config=current_config)
        await self._approval_recovery.mark_startup_runtime_support_ready()
        self._bind_trigger_runtimes()
        await self._emit_config_reloaded(
            new_config=new_config,
            changed_entities=changed_entities,
            added_entities=added_entities,
            removed_entities=removed_entities,
            plugin_changes=plugin_changes,
        )

    async def _apply_config_update_plan(self, current_config: Config, plan: ConfigUpdatePlan) -> bool:
        """Apply one computed config update plan: stop what changes, publish the new config, then start."""
        new_config = plan.new_config
        await self._prepare_accounts_for_config_update(new_config, plan)
        await self._script_runtime.apply_update_plan(plan)
        try:
            async with self._paused_startup_maintenance():
                commit_plugins = self._prepare_plugins(plan)
                # Removal first, while the current router still delivers its approval notices
                # and before a replacement router's orphan cleanup could kick the removed accounts.
                unsettled_removals = await self.fleet.remove(plan.remove)
                await self.fleet.stop(plan.restart)
                self._publish_config(plan, commit_plugins)
                # Now that the removal is published, recovery can judge the removed owners as gone.
                await self._approval_recovery.reconcile_unavailable_entities(unsettled_removals)
                await self._script_runtime.complete_worker_replacement()
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
                await self._update_unchanged_bots(new_config)
                if plan.router_invite_policy_changed and ROUTER_AGENT_NAME not in plan.restart:
                    router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
                    if router_bot is not None:
                        router_bot.schedule_pending_invite_reconciliation()

                if plan.only_support_service_changes and not catalog_restarts:
                    await self._finalize_config_reload(
                        new_config=new_config,
                        current_config=current_config,
                        changed_entities=set(plan.live_updated),
                        added_entities=set(plan.start),
                        removed_entities=set(plan.remove),
                        plugin_changes=plan.plugin_changes,
                    )
                    return False

                changed_entities = set(plan.restart | plan.start | catalog_restarts)
                await self.fleet.create(changed_entities, new_config)
                start_results = await self.fleet.start(changed_entities)
                await self._reconcile_post_update_rooms(plan, changed_entities)
                if start_results.permanently_failed_entities:
                    logger.warning(
                        "Configuration update left some bots disabled due to permanent startup errors",
                        agent_names=start_results.permanently_failed_entities,
                    )

                await self._finalize_config_reload(
                    new_config=new_config,
                    current_config=current_config,
                    changed_entities=changed_entities | plan.live_updated,
                    added_entities=set(plan.start),
                    removed_entities=set(plan.remove),
                    plugin_changes=plan.plugin_changes,
                )
                logger.info("configuration_update_complete", affected_bot_count=len(changed_entities))
                return True
        finally:
            await self._script_runtime.complete_worker_replacement()

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

    async def _bind_event_journal(self) -> None:
        """Refuse startup unless the configured database is this install's journal.

        Runs before any bot is created, so a store this install is not bound to
        is never written to: opening a stranger's journal would take over its
        outbox and recovery work, and opening a fresh one would replay and
        re-answer a past that already happened.
        """
        config = self._require_config()
        store = self._shared_journal_store()
        identity = await bind_event_journal(
            store,
            journal_config=config.event_journal,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
        )
        # Storage roots bound to one database share its identity: a second
        # runtime against it would answer every message again.
        if not await store.hold_exclusively(identity):
            msg = "Another MindRoom runtime is already using this event journal"
            raise PermanentStartupError(msg)
        self._journal_held = True

    async def event_journal_still_held(self) -> bool:
        """Return whether this runtime still holds the journal it claimed; true before it claims one."""
        journal = self._open_journal
        return journal is None or not self._journal_held or await journal.store.still_held()

    def _shared_journal_store(self) -> EventJournalStore:
        """Return the one journal store every bot in this process borrows.

        One database holds every principal, so opening it per bot bought
        nothing and cost a connection pool each: on PostgreSQL that multiplies
        connections by the number of configured entities until the server
        refuses them, and on SQLite it moves write contention off an in-process
        queue and onto the file lock. Bots borrow this and do not close it.
        """
        if self._open_journal is None:
            config = self.config
            if config is None:
                msg = "The orchestrator needs its config before it can open the event journal"
                raise RuntimeError(msg)
            self._open_journal = open_event_journal(
                config.event_journal,
                runtime_paths=self.runtime_paths,
                storage_path=self.storage_path,
            )
        return self._open_journal.store

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        self._runtime_ready_event.clear()
        self.fleet.begin_process_shutdown()
        self.agent_cli_registry.close()
        self.hook_registry = HookRegistry.empty()
        set_scheduling_hook_registry(self.hook_registry)
        if self._runtime_shutdown_event is not None:
            self._runtime_shutdown_event.set()
        self._external_trigger_runtime.unbind()
        self._computer_runtime.unbind()
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
                owner=self._dispatch_recovery_task_owner,
                shutdown_intent=ORDERLY_SHUTDOWN,
            ),
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
        self.invalidate_agent_reply_memberships(reason="shutdown")
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


async def _handle_config_change(orchestrator: _MultiAgentOrchestrator) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed; queueing hot reload")
    orchestrator.config_reload.request_reload()


async def _watch_config_task(config_path: Path, orchestrator: _MultiAgentOrchestrator) -> None:
    """Watch the config file plus every file it pulls in via !include."""

    def config_source_paths() -> Iterable[Path]:
        config = orchestrator.config
        watched: set[Path] = {config_path}
        loaded_source_files = orchestrator.config_reload.loaded_source_files
        if loaded_source_files is not None:
            watched.update(loaded_source_files)
        elif config is not None and config.source_files:
            watched.update(config.source_files)
        # A failed reload's own source set covers include files the last good
        # config never referenced, so fixing them still triggers a retry.
        failed_source_files = orchestrator.config_reload.failed_reload_source_files
        if failed_source_files:
            watched.update(failed_source_files)
        return watched

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator)

    await file_watcher.watch_paths(config_source_paths, on_config_change)


async def _watch_skills_task(orchestrator: _MultiAgentOrchestrator) -> None:
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


async def _run_api_server(  # noqa: PLR0915 - the primary API and its worker-facing listeners share one lifecycle
    host: str,
    port: int,
    log_level: str,
    runtime_paths: RuntimePaths,
    knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    script_runtime: ScriptRuntimeLifecycle | None = None,
    shutdown_requested: asyncio.Event | None = None,
    *,
    thread_export_runner: WorkspaceThreadExportRunner | None = None,
    leave_matrix_room: Callable[[str, str], Awaitable[bool]] | None = None,
    response_admission_gate: ResponseAdmissionGate | None = None,
    active_calls: Callable[[], list[ResponseIdentity]] | None = None,
    config_reload_status: Callable[[], ConfigReloadStatus] | None = None,
    agent_reply_memberships: AgentReplyMembershipIndex | None = None,
    agent_cli_registry: TurnToolRegistry | None = None,
    budget_monitor: BudgetMonitor | None = None,
) -> None:
    """Run the bundled dashboard/API server as an asyncio task."""
    from mindroom.api import main as api_main  # noqa: PLC0415
    from mindroom.api.agent_cli import bind_agent_cli_registry  # noqa: PLC0415
    from mindroom.api.script_gateway import serve_script_gateway_listener  # noqa: PLC0415
    from mindroom.egress_broker.service import serve_egress_broker  # noqa: PLC0415

    api_server = _EmbeddedApiServerContext(host=host, port=port)
    bind_agent_cli_registry(api_main.app, agent_cli_registry)
    api_state = api_main.config_lifecycle.app_state(api_main.app)
    api_state.thread_export_runner = thread_export_runner
    api_state.leave_matrix_room = leave_matrix_room
    api_state.response_admission_gate = response_admission_gate
    api_state.active_calls = active_calls
    api_state.active_script_runs = script_runtime.active_runs if script_runtime is not None else None
    api_state.config_reload_status = config_reload_status
    api_state.budget_monitor = budget_monitor
    if agent_reply_memberships is not None:
        api_state.agent_reply_memberships = agent_reply_memberships
    if script_runtime is not None:
        api_main.bind_script_runtime(
            api_main.app,
            broker=script_runtime.broker,
            touch_live_workers=script_runtime.touch_live_workers,
        )
    if knowledge_refresh_scheduler is not None:
        api_main.bind_orchestrator_knowledge_refresh_scheduler(api_main.app, knowledge_refresh_scheduler)
    config = uvicorn.Config(
        api_main.app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        ws="websockets-sansio",
    )

    async def on_started(bound_host: str, bound_port: int) -> None:
        if script_runtime is not None:
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
                    broker=None if script_runtime is None else script_runtime.broker,
                    log_level=log_level,
                    agent_cli_registry=agent_cli_registry,
                ),
            ):
                await server.serve()
        except SystemExit as exc:
            _raise_embedded_api_server_exit(api_server, reason="server.serve() raised SystemExit", cause=exc)
    finally:
        bind_agent_cli_registry(api_main.app, None)
        api_state.orchestrator_knowledge_refresh_scheduler = None
        api_state.thread_export_runner = None
        api_state.leave_matrix_room = None
        api_state.response_admission_gate = None
        api_state.active_calls = None
        api_state.active_script_runs = None
        api_state.config_reload_status = None
        api_state.budget_monitor = None
        api_state.agent_reply_memberships = AgentReplyMembershipIndex()
        if script_runtime is not None:
            await script_runtime.unbind_api()
            api_main.unbind_script_runtime(api_main.app)
        clear_api_server_address()
    shutdown_expected = shutdown_requested.is_set() if shutdown_requested is not None else False
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


def _sync_credentials_and_prepare_storage(runtime_paths: RuntimePaths, storage_path: Path) -> None:
    """Complete the synchronous startup setup that must precede runtime construction."""
    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials(runtime_paths=runtime_paths)
    storage_path.mkdir(parents=True, exist_ok=True)


async def _watch_event_journal_hold(
    orchestrator: _MultiAgentOrchestrator,
    shutdown_requested: asyncio.Event,
    hold_lost: asyncio.Event,
) -> None:
    """Stop the runtime once it no longer holds its event journal, before another runtime can take it over."""
    while not shutdown_requested.is_set():
        await asyncio.sleep(_EVENT_JOURNAL_HOLD_CHECK_SECONDS)
        if not await orchestrator.event_journal_still_held():
            logger.error("event_journal_hold_lost")
            hold_lost.set()
            shutdown_requested.set()
            return


def _start_auxiliary_tasks(
    orchestrator: _MultiAgentOrchestrator,
    runtime_paths: RuntimePaths,
    shutdown_requested: asyncio.Event,
    journal_hold_lost: asyncio.Event,
) -> list[asyncio.Task]:
    """Start the non-critical background tasks that run beside the orchestrator."""
    # First, so an invalid heap task interval fails before any task exists that shutdown could not cancel.
    heap_tasks = start_heap_tasks(runtime_paths, runtime_ready=orchestrator.wait_until_runtime_ready)
    auxiliary_specs = [
        (
            "config watcher",
            lambda: _watch_config_task(orchestrator.config_path, orchestrator),
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
            _watch_event_journal_hold(orchestrator, shutdown_requested, journal_hold_lost),
            name="event_journal_hold_watch",
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
    await migrate_private_storage(runtime_paths)
    await migrate_state_root_records(runtime_paths)
    await migrate_tool_credential_defaults(runtime_paths)
    storage_path = runtime_paths.storage_root
    orchestrator: _MultiAgentOrchestrator | None = None
    auxiliary_tasks: list[asyncio.Task] = []
    shutdown_requested = asyncio.Event()
    journal_hold_lost = asyncio.Event()
    api_server = _EmbeddedApiServerContext(host=api_host, port=api_port)
    orchestrator_task: asyncio.Task[None] | None = None
    shutdown_wait_task: asyncio.Task[bool] | None = None
    api_task: asyncio.Task[None] | None = None
    stall_detector: EventLoopStallDetector | None = None

    try:
        # Drop any stale worker manager before startup work builds the active runtime.
        reset_primary_worker_manager()

        # Configure logging before any background tasks or account setup begin.
        setup_logging(level=log_level, runtime_paths=runtime_paths)

        shorten_gil_switch_interval()
        stall_detector = start_event_loop_stall_detector(runtime_paths)

        # Credential synchronization and storage setup are synchronous. Keep the
        # ordered unit off-loop while retaining exception propagation to startup.
        await run_blocking_until_complete(_sync_credentials_and_prepare_storage, runtime_paths, storage_path)
        await run_blocking_until_complete(retire_state_root_worker_mounts, runtime_paths)
        # After the retirement, so no old worker mounting whole state roots can swap a sessions directory as it converts.
        # The first start after an upgrade converts every conversation database before the API binds, which can take
        # minutes, so probes meanwhile see a live process that is not ready yet instead of a closed port.
        probes = serve_startup_probes(api_host, api_port, detail="Converting conversation storage") if api else None
        with probes or nullcontext():
            await migrate_conversation_storage(runtime_paths)
            # Before the API serves and before the homeserver answers, so the dashboard has this one load meanwhile.
            startup_config = await load_startup_config(runtime_paths, api_enabled=api)

        logger.info("Starting orchestrator...")
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, api_enabled=api)
        set_runtime_starting()
        auxiliary_tasks.extend(
            _start_auxiliary_tasks(orchestrator, runtime_paths, shutdown_requested, journal_hold_lost),
        )

        if api:
            api_task = asyncio.create_task(
                _run_api_server(
                    api_host,
                    api_port,
                    log_level,
                    runtime_paths,
                    orchestrator.knowledge_refresh_scheduler,
                    orchestrator.script_runtime,
                    shutdown_requested,
                    thread_export_runner=orchestrator._thread_export_runner,
                    leave_matrix_room=orchestrator.leave_matrix_room,
                    response_admission_gate=orchestrator._response_admission_gate,
                    active_calls=orchestrator.active_call_identities,
                    config_reload_status=lambda: orchestrator.config_reload.status,
                    agent_reply_memberships=orchestrator.agent_reply_memberships,
                    agent_cli_registry=orchestrator.agent_cli_registry,
                    budget_monitor=orchestrator.budgets,
                ),
                name="api_server",
            )
        elif egress_broker_enabled(runtime_paths):
            logger.warning(
                "egress_broker_not_started",
                reason="the egress broker runs with the embedded API server, which is disabled",
            )

        orchestrator_task = asyncio.create_task(orchestrator.start(startup_config), name="orchestrator")
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
        await _wait_for_runtime_shutdown_cleanup(
            cleanup_task,
            shutdown_was_requested=shutdown_was_requested,
        )
    if journal_hold_lost.is_set():
        # Exiting with a failure lets a supervisor that restarts only on failure bring the runtime back.
        msg = "MindRoom stopped because it no longer holds its event journal"
        raise EventJournalHoldLostError(msg)
