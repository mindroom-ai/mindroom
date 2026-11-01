"""Matrix runtime shell for agents, teams, and the router."""

from __future__ import annotations

import asyncio
import random
import time
from contextvars import Context
from functools import cached_property, partial
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

import nio
from nio import AuthenticatedToDeviceEvent

from mindroom.approval.inbound import handle_tool_approval_response_event
from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.bot_runtime_view import BotRuntimeState, RunPhase
from mindroom.config.resolution import entity_timezone
from mindroom.desktop.pairing_receiver import register_desktop_pairing_receiver
from mindroom.event_journal.identity import journal_principal_id
from mindroom.event_journal.legacy_turn_rows import legacy_responses_file_path
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookContextSupport,
    HookRegistry,
    HookRegistryLifetime,
    emit,
)
from mindroom.hooks.ingress import IngressHookRunner
from mindroom.integrations import reply_mirror_for
from mindroom.matrix.decrypt_diagnostics import DecryptionDiagnostics
from mindroom.matrix.durable_ingestion import run_ingestion_pump
from mindroom.matrix.durable_membership import change_local_membership
from mindroom.matrix.health import (
    clear_matrix_sync_state,
    mark_matrix_sync_loop_started,
    mark_matrix_sync_success,
)
from mindroom.matrix.invited_rooms_store import read_invited_rooms
from mindroom.matrix.personal_rooms import PersonalRoomService
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.state import resolve_room_aliases
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_loop import bot_ingestion_config, subscribe_sliding_rooms
from mindroom.matrix.users import AgentMatrixUser, login_agent_owned_session
from mindroom.matrix_rtc.call_manager import CallManager, maybe_build_call_manager
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.model_catalog_receiver import register_model_catalog_receiver
from mindroom.orchestration.server_state import publish_server_state, server_state_content
from mindroom.personal_room_lifecycle import PersonalRoomLifecycle, PersonalRoomTarget
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.runtime_shutdown import (
    GENERIC_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    RESPONSE_FINALIZATION_TIMEOUT_SECONDS,
    SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS,
    ResponseShutdownTimeoutError,
    RuntimeShutdownIntent,
    ShutdownBudget,
    restart_reason_category_for,
)
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeSupport

from . import constants
from .agent_cli.cli_approval_waits import CliWaiters
from .approval.continuation_runner import ApprovalContinuationRunner
from .background_tasks import create_background_task, wait_for_background_tasks
from .coalescing.gate import CoalescingGate
from .command_turn_executor import CommandTurnExecutor, CommandTurnExecutorDeps
from .commands import config_confirmation
from .constants import ROUTER_AGENT_NAME, RuntimePaths
from .conversation_resolver import ConversationResolver, ConversationResolverDeps
from .conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from .delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    SendTextRequest,
)
from .edit_regenerator import EditRegenerator, EditRegeneratorDeps
from .entity_rooms import get_rooms_for_entity
from .event_journal import (
    EventKind,
    HistoryRecoveryState,
    PrincipalStore,
)
from .inbound_turn_normalizer import InboundTurnNormalizer, InboundTurnNormalizerDeps
from .ingress_validation import IngressValidator, IngressValidatorDeps
from .journal_dispatch import (
    JournalCallbacks,
    JournalDispatcher,
)
from .knowledge.utils import KnowledgeAccessSupport
from .logging_config import get_logger
from .managed_avatars import ensure_entity_avatar
from .matrix.client_room_admin import get_joined_rooms
from .matrix.client_session import PermanentMatrixStartupError
from .matrix.conversation_hydration import ConversationHydrator
from .matrix.conversation_reads import ConversationReader
from .matrix.relation_lookup import RelationLookup
from .reaction_dispatch import ReactionDispatcher, ReactionDispatcherDeps
from .reply_scope import ReplyRuntime
from .reply_turn.inbox import InboxResponses
from .reply_turn.locked_turn import LockedTurns, ReplyTurnDeps
from .response_lifecycle import ResponseHookService, ResponseLifecycleCoordinator
from .response_payload_preparation import ResponsePayloadPreparer
from .response_runner import ResponseRunner
from .response_shutdown_diagnostics import DeferredStopPhase
from .router.duties import RouterDuties, RouterDutiesDeps
from .tool_jobs.client_cancel import cancel_room_jobs, handle_job_cancel_event
from .tool_jobs.follow_ups import follow_up_event
from .tool_jobs.runtime import get_background_runtime
from .turn_policy import TurnPolicy, TurnPolicyDeps
from .turn_store import TurnStore, TurnStoreDeps
from .turns.dispatch import TurnDispatchDeps, TurnDispatcher
from .turns.ingress import TurnIngress, TurnIngressDeps
from .turns.interactive_selection import InteractiveSelections, InteractiveSelectionsDeps
from .user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from .visible_response_reconciliation import VisibleResponseReconciler, VisibleResponseReconcilerDeps
from .voice.ingress import VoiceIngress, VoiceIngressDeps
from .voice.readiness import VoiceReadiness
from .voice.visible_voice_echo import VisibleVoiceEchoDeps, VisibleVoiceEchoLifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime
    from pathlib import Path

    import structlog
    from nio.durable import DurableSync

    from mindroom.config.main import Config
    from mindroom.event_journal import AdmissionFacts, IngestionRecordAdmission
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.tool_jobs.conversation import ConversationKey

    from .dispatch_callback_outcome import TurnDispatchOutcome


class _ProcessShutdownMatrixClient(Protocol):
    """Owned Matrix transport boundary used only during process shutdown."""

    def begin_process_shutdown_transport_fence(self) -> None:
        """Permanently refuse new Matrix transport attempts."""


logger = get_logger(__name__)

__all__ = ["AgentBot", "create_bot_for_entity"]


# Constants
# Clean restart waits for one final source response before cancelling the
# receive loop. Keep that poll comfortably inside the 15-second restart budget
# so shutdown still leaves time for the replacement to boot and authenticate.
_SYNC_TIMEOUT_MS = 5_000
_DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS = 1.0
_DELIVERY_RECOVERY_RETRY_MAX_DELAY_SECONDS = 30.0
# Startup recovery applies turn retention once. A running bot repeats that pass
# this often so the turn tables stay near the retention bound between restarts.
_HANDLED_TURN_CLEANUP_INTERVAL_SECONDS = 60 * 60


_SYNC_FILTER: dict[str, object] = {
    "room": {"timeline": {"limit": constants.CLASSIC_SYNC_TIMELINE_LIMIT}},
}


def _create_best_effort_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    *,
    owner: BotRuntimeState,
) -> Callable[..., Awaitable[None]]:
    """Run one explicitly best-effort callback as a background task.

    Use this only for auxiliary consumers or Matrix inputs without a stable
    source event ID.
    Correctness-critical source-backed events are admitted to the event journal.
    """

    async def wrapper(*args: object, **kwargs: object) -> None:
        # Create the task but don't await it - let it run in background
        async def error_handler() -> None:
            try:
                await callback(*args, **kwargs)
            except asyncio.CancelledError:
                # Task was cancelled, this is expected during shutdown
                pass
            except Exception:
                # Log the exception with full traceback
                logger.exception("Error in event callback")

        # Keep a strong reference via background task registry.
        create_background_task(error_handler(), owner=owner)

    return wrapper


def create_bot_for_entity(
    entity_name: str,
    agent_user: AgentMatrixUser,
    orchestrator: OrchestratorRuntime,
) -> AgentBot:
    """Create the bot for one configured entity, the router, a team, or an agent, in its configured rooms.

    Raises:
        ValueError: The published config does not configure the entity.

    """
    config = orchestrator.config
    if entity_name != ROUTER_AGENT_NAME and entity_name not in config.agents and entity_name not in config.teams:
        msg = f"Entity '{entity_name}' not found in configuration."
        raise ValueError(msg)
    rooms = resolve_room_aliases(get_rooms_for_entity(entity_name, config), orchestrator.runtime_paths)
    return AgentBot(agent_user, rooms=rooms, orchestrator=orchestrator)


class AgentBot:
    """Matrix lifecycle shell for one configured agent or router entity."""

    # Construction inputs
    agent_user: AgentMatrixUser
    storage_path: Path
    runtime_paths: RuntimePaths
    rooms: list[str]
    logger: structlog.stdlib.BoundLogger

    # Mutable lifecycle state
    running: bool
    last_sync_time: datetime | None
    _last_sync_monotonic: float | None
    _first_sync_done: bool
    _sync_shutdown_budget: ShutdownBudget | None
    _deferred_stop_required: bool
    _deferred_stop_phase: DeferredStopPhase | None
    _delivery_recovery_wake: asyncio.Event
    _delivery_projection_progress: asyncio.Event
    _delivery_recovery_task: asyncio.Task[None] | None
    _next_handled_turn_cleanup_at: float
    _ingestion_session: DurableSync | None

    # Shared runtime state and extracted collaborators
    _hook_registry_state: HookRegistryLifetime
    _runtime_view: BotRuntimeState
    _coalescing_gate: CoalescingGate
    _inbound_turn_normalizer: InboundTurnNormalizer
    _turn_policy: TurnPolicy
    _conversation_resolver: ConversationResolver
    _conversation_state_writer: ConversationStateWriter
    _delivery_gateway: DeliveryGateway
    _response_runner: ResponseRunner
    _turn_store: TurnStore
    _visible_voice_echo: VisibleVoiceEchoLifecycle
    _tool_runtime_support: ToolRuntimeSupport
    _post_response_effects_support: PostResponseEffectsSupport
    _ingress_hook_runner: IngressHookRunner
    _request_payload_preparer: ResponsePayloadPreparer
    _hook_context_support: HookContextSupport
    _knowledge_access_support: KnowledgeAccessSupport
    _call_manager: CallManager | None
    _calls_full_reconcile_pending: bool
    _call_rooms_reconcile_pending: set[str]
    _router: RouterDuties | None
    _interactive_selections: InteractiveSelections
    _voice_ingress: VoiceIngress
    _turn_dispatcher: TurnDispatcher
    _turn_ingress: TurnIngress
    _room_lifecycle: BotRoomLifecycle
    _decryption_diagnostics: DecryptionDiagnostics
    _local_membership_lock: asyncio.Lock
    _ingestion_admission_progress: asyncio.Event
    _sync_continuity_store: SyncContinuityStore

    def __init__(
        self,
        agent_user: AgentMatrixUser,
        *,
        rooms: list[str],
        orchestrator: OrchestratorRuntime,
    ) -> None:
        """Build one bot generation, which reads the config, gate, membership index, and journal its runtime owns."""
        self._approval_runtime_generation = uuid4().hex
        self.agent_user = agent_user
        self.runtime_paths = orchestrator.runtime_paths
        self.storage_path = self.runtime_paths.storage_root
        self.rooms = rooms
        self.logger = logger.bind(agent=self.agent_name)
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._orchestrator_ready_handled = False
        # The Matrix device this bot sends as, captured at login rather than
        # read off the client per send. A transaction ID only deduplicates
        # within the device that used it, so the outbox records this next to
        # every claim; before login there is no answer, and `None` says so.
        self._sending_device_id: str | None = None
        self._sync_shutdown_budget = None
        self._deferred_stop_required = False
        self._deferred_stop_phase = None
        self._delivery_recovery_wake = asyncio.Event()
        self._delivery_projection_progress = asyncio.Event()
        self._delivery_recovery_task = None
        # Jitter the first pass so bots started together do not all prune at once.
        self._next_handled_turn_cleanup_at = time.monotonic() + _HANDLED_TURN_CLEANUP_INTERVAL_SECONDS * (
            1.0 + random.random()  # noqa: S311
        )
        self._ingestion_session = None
        # This generation's view of the runtime's hook registry; its stop ends it, so hooks it bound turn inactive.
        self._hook_registry_state = orchestrator.hook_registry_state.lifetime()
        self._runtime_view = BotRuntimeState(orchestrator=orchestrator)
        self._sync_continuity_store = SyncContinuityStore(self.storage_path, self.agent_name)
        self._call_manager: CallManager | None = None
        self._calls_full_reconcile_pending = False
        self._call_rooms_reconcile_pending = set()
        self._local_membership_lock = asyncio.Lock()
        self._ingestion_admission_progress = asyncio.Event()

        async def send_room_lifecycle_response(
            *,
            target: MessageTarget,
            response_text: str,
            skip_mentions: bool = False,
        ) -> str | None:
            return await self._delivery_gateway.send_text(
                SendTextRequest(
                    target=target,
                    response_text=response_text,
                    skip_mentions=skip_mentions,
                ),
            )

        self.personal_rooms = PersonalRoomService(
            self.agent_name,
            self._runtime_view,
            self.runtime_paths,
            self.change_local_membership,
        )
        self._personal_room_lifecycle = PersonalRoomLifecycle(
            self.agent_name,
            self._runtime_view,
            self.runtime_paths,
            self.personal_rooms,
            self._lookup_personal_room_target,
            orchestrator.config_reload,
            self._report_personal_room_membership_change,
        )
        self._room_lifecycle = BotRoomLifecycle(
            BotRoomLifecycleDeps(
                agent_name=self.agent_name,
                agent_user=self.agent_user,
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                continuity_store=self._sync_continuity_store,
                get_logger=lambda: self.logger,
                get_configured_rooms=lambda: self.rooms,
                get_retained_room_ids=self._personal_room_lifecycle.retained_room_ids,
                get_cleanup_exclusions=self._personal_room_lifecycle.cleanup_exclusions,
                send_response=send_room_lifecycle_response,
                change_membership=self.change_local_membership,
                admit_response=self._runtime_view.admission_fence,
                on_configured_room_joined=self._post_join_room_setup,
                on_room_joined=self._restore_scheduled_tasks,
                get_conversation_reader=lambda: self._conversation_reader,
            ),
        )
        self._init_runtime_components()

    def journal_principal(self) -> PrincipalStore:
        """Return this bot's principal-bound projection view, once the journal is open."""
        return self._journal_principal

    async def redacted_event_ids(self, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
        """Return which of a room's events this bot saw redacted, for readers of its stored conversations.

        Redactions inside a gap its sync skipped reach the journal only when the room is repaired, so the lookup runs
        that repair first, as a strict read of the room would.
        """
        recovery = await self.journal_principal().room_history_recovery(room_id)
        if recovery is not None and recovery.state is HistoryRecoveryState.REPAIRABLE:
            await self._conversation_reader.hydrator.ensure_hydrated(room_id=room_id, thread_id=None)
        return await self._turn_store.redacted_event_ids(room_id, event_ids)

    def _init_runtime_components(self) -> None:
        """Initialize runtime-only helpers that depend on bound instance methods."""
        if not self.agent_user.user_id:
            msg = f"Missing Matrix ID for {self.agent_name!r} during runtime initialization"
            raise PermanentMatrixStartupError(msg)
        runtime_matrix_id = self.matrix_id
        self._journal_principal_id = journal_principal_id(self.agent_name, runtime_matrix_id.full_id)
        # One database holds every principal, so the runtime's one store serves this bot's view. The runtime closes it.
        self._journal_store = self.orchestrator.event_journal_store()
        principal = self._journal_store.principal(self._journal_principal_id)
        self._journal_principal = principal

        def format_local_timestamp(timestamp_ms: float | None) -> str | None:
            return format_timestamp_ms(timestamp_ms, timezone=entity_timezone(self.config, self.agent_name))

        self._knowledge_access_support = KnowledgeAccessSupport(
            runtime=self._runtime_view,
            runtime_paths=self.runtime_paths,
        )
        self._conversation_state_writer = ConversationStateWriter(
            ConversationStateWriterDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
            ),
        )
        self._relations = RelationLookup(
            store=principal,
            runtime=self._runtime_view,
        )
        self._conversation_reader = ConversationReader(
            store=principal,
            hydrator=ConversationHydrator(
                store=principal,
                runtime=self._runtime_view,
                self_sender=runtime_matrix_id.full_id,
            ),
        )
        self._conversation_resolver = ConversationResolver(
            ConversationResolverDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                conversation_reader=self._conversation_reader,
                relations=self._relations,
            ),
        )
        self._inbound_turn_normalizer = InboundTurnNormalizer(
            InboundTurnNormalizerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                storage_path=self.storage_path,
                runtime_paths=self.runtime_paths,
            ),
        )
        self._turn_policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                agent_reply_memberships=self._runtime_view.agent_reply_memberships,
            ),
        )
        self._hook_context_support = HookContextSupport(
            runtime=self._runtime_view,
            runtime_paths=self.runtime_paths,
            agent_name=self.agent_name,
            hook_registry_state=self._hook_registry_state,
            conversation_reader=self._conversation_reader,
            logger=self.logger,
        )
        self._router = (
            None
            if self.agent_name != ROUTER_AGENT_NAME
            else RouterDuties(
                RouterDutiesDeps(
                    runtime=self._runtime_view,
                    logger=self.logger,
                    matrix_id=runtime_matrix_id,
                    hook_context=self._hook_context_support,
                    journal=principal,
                    room_lifecycle=self._room_lifecycle,
                    personal_rooms=self._personal_room_lifecycle,
                ),
            )
        )
        self._tool_runtime_support = ToolRuntimeSupport(
            runtime=self._runtime_view,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
            agent_name=self.agent_name,
            matrix_id=runtime_matrix_id,
            resolver=self._conversation_resolver,
            hook_context=self._hook_context_support,
            membership=principal,
        )
        self._journal_dispatcher = JournalDispatcher(
            store=principal,
            # Owners built below hand their sources back through this dispatcher, so a callback that reaches one is a
            # bot routing method or resolves its owner when it runs.
            callbacks=JournalCallbacks(
                on_message=self._on_message,
                on_media=self._on_media_message,
                on_reaction=self._on_reaction,
                on_approval=partial(
                    handle_tool_approval_response_event,
                    runtime=self._runtime_view,
                    runtime_paths=self.runtime_paths,
                    logger=self.logger,
                ),
                on_job_cancel=partial(
                    handle_job_cancel_event,
                    agent_name=self.agent_name,
                    runtime=self._runtime_view,
                    runtime_paths=self.runtime_paths,
                    may_talk=self._turn_policy.can_reply_to_sender_in_room,
                ),
                on_room_lifecycle=self._on_room_member,
                on_rtc=self._on_rtc_event,
                on_redaction=self._on_redaction,
                on_approval_continuation=lambda event_id: self._response_runner.handoff_approval_source(event_id),
                source_has_live_owner=lambda event_id: (
                    self._coalescing_gate.has_pending_source_event(event_id)
                    or self._response_runner.inbox.has_live_inbox_response(event_id)
                ),
                turn_has_live_claim=lambda event_id: self._turn_store.has_live_turn_claim(event_id),
                replies_ended=self._replies_ended,
                on_job_follow_up=lambda event: self._response_runner.handoff_job_follow_up(event),
            ),
            room_for_id=self._room_for_journal_event,
            # Resolved late: the ingress validator, which owns the trust rule, is built after the dispatcher.
            schedule_trigger_sender_is_managed=lambda sender: (
                self._ingress_validator.sender_is_trusted_for_ingress_metadata(sender)
            ),
            runtime_generation=self._approval_runtime_generation,
        )
        self._turn_store = TurnStore(
            TurnStoreDeps(
                turn_records=self._journal_store.turn_records(self.agent_name),
                legacy_responses_file=legacy_responses_file_path(self.storage_path, self.agent_name),
                state_writer=self._conversation_state_writer,
                tool_runtime=self._tool_runtime_support,
                response_recovery_state=principal.response_recovery_state,
                retry_dispatch_sources=self._journal_dispatcher.retry_turn_sources,
            ),
        )
        self._reply_runtime = ReplyRuntime(
            store=principal,
            entity_name=self.agent_name,
            generation=self._approval_runtime_generation,
            retry_sources=self._journal_dispatcher.retry_turn_sources,
            # The reply runtime and the delivery gateway hold each other through the reply-debt methods
            # `_settle_reply_debt_later` and `_reply_row_resolved`.
            approval_ended=self._settle_reply_debt_later,
        )
        self._delivery_gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                logger=self.logger,
                resolver=self._conversation_resolver,
                response_hooks=ResponseHookService(
                    hook_context=self._hook_context_support,
                    # Reads the live config, which a reload replaces.
                    reply_mirror=lambda envelope, response_text: reply_mirror_for(
                        envelope,
                        response_text,
                        config=self.config,
                        runtime_paths=self.runtime_paths,
                    ),
                ),
                outbox=principal,
                turn_handoff=self._journal_dispatcher.release_delivered_turn_sources,
                # Read when each send is claimed: this gateway is built before
                # the bot has logged in, and a re-login replaces the device
                # that the frozen transaction IDs belong to.
                sending_device_id=lambda: self._sending_device_id,
                reply_effects=self._reply_runtime.run_effects,
                reply_row_resolved=self._reply_row_resolved,
            ),
        )
        self._post_response_effects_support = PostResponseEffectsSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            conversation_reader=self._conversation_reader,
            membership=principal,
            agent_name=self.agent_name,
        )
        self._ingress_hook_runner = IngressHookRunner(
            hook_context=self._hook_context_support,
        )
        self._request_payload_preparer = ResponsePayloadPreparer(
            normalizer=self._inbound_turn_normalizer,
            ingress_hook_runner=self._ingress_hook_runner,
            agent_name=self.agent_name,
            logger=self.logger,
        )
        reply_turn_deps = ReplyTurnDeps(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            agent_name=self.agent_name,
            matrix_full_id=runtime_matrix_id.full_id,
            resolver=self._conversation_resolver,
            tool_runtime=self._tool_runtime_support,
            knowledge_access=self._knowledge_access_support,
            delivery_gateway=self._delivery_gateway,
            post_response_effects=self._post_response_effects_support,
            state_writer=self._conversation_state_writer,
            request_preparer=self._request_payload_preparer,
            approval_store=principal.approvals,
            retry_approval_sources=self.retry_approval_sources,
            settle_ignored_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
            approval_runtime_generation=self._approval_runtime_generation,
            redacted_history_events=self._turn_store.redacted_history_events,
            replies=self._reply_runtime,
        )
        self._build_reply_turn_owners(reply_turn_deps)
        self._decryption_diagnostics = DecryptionDiagnostics(
            agent_name=self.agent_name,
            runtime=self._runtime_view,
            runtime_paths=self.runtime_paths,
            read_position=principal.ingestion_membership_position,
            admit_response=self._runtime_view.admission_fence,
            notice_is_fenced=self._room_lifecycle.decrypt_notice_is_fenced,
        )
        self._edit_regenerator = EditRegenerator(
            EditRegeneratorDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                resolver=self._conversation_resolver,
                turn_store=self._turn_store,
                ingress_hook_runner=self._ingress_hook_runner,
                response_runner=self._response_runner,
                turn_policy=self._turn_policy,
                settle_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
                stop_reply=lambda reply, receipt_order: self._delivery_gateway.stop_reply_by_id(
                    reply.reply_id,
                    receipt_order,
                ),
                receipt_order=self._journal_dispatcher.receipt_order,
                timestamp_formatter=format_local_timestamp,
                store=principal,
            ),
        )
        self._ingress_validator = IngressValidator(
            IngressValidatorDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                turn_store=self._turn_store,
                turn_policy=self._turn_policy,
                settle_ignored_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
            ),
        )
        self._visible_voice_echo = VisibleVoiceEchoLifecycle(
            VisibleVoiceEchoDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                agent_name=self.agent_name,
                delivery_gateway=self._delivery_gateway,
                turn_store=self._turn_store,
                router_turn_records=self._journal_store.turn_records(ROUTER_AGENT_NAME),
                ingress=self._ingress_validator,
            ),
        )
        self._visible_responses = VisibleResponseReconciler(
            VisibleResponseReconcilerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                response_sender=runtime_matrix_id.full_id,
                turn_store=self._turn_store,
                delivery_gateway=self._delivery_gateway,
                settle_ignored_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
                replies=self._reply_runtime,
            ),
        )
        self._command_turn_executor = CommandTurnExecutor(
            CommandTurnExecutorDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                normalizer=self._inbound_turn_normalizer,
                turn_policy=self._turn_policy,
                turn_store=self._turn_store,
                visible_responses=self._visible_responses,
                conversation_reader=self._conversation_reader,
                recover_config_confirmation_setup=self._recover_config_confirmation_setup,
                controller_identity=self.orchestrator.desktop_controller_identity,
            ),
        )
        self._user_stop_reconciler = UserStopReconciler(
            UserStopReconcilerDeps(
                turn_store=self._turn_store,
                delivery_gateway=self._delivery_gateway,
            ),
        )
        self._interactive_selections = InteractiveSelections(
            InteractiveSelectionsDeps(
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                resolver=self._conversation_resolver,
                normalizer=self._inbound_turn_normalizer,
                ingress=self._ingress_validator,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                turn_store=self._turn_store,
                visible_responses=self._visible_responses,
                retry_dispatch_sources=self._journal_dispatcher.retry_turn_sources,
                settle_dispatch_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
                dispatch_source_is_terminal=self._journal_dispatcher.source_is_terminal,
            ),
        )
        self._voice_ingress = VoiceIngress(
            VoiceIngressDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                matrix_id=runtime_matrix_id,
                resolver=self._conversation_resolver,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                readiness=VoiceReadiness(
                    normalizer=self._inbound_turn_normalizer,
                    turn_store=self._turn_store,
                    visible_echo=self._visible_voice_echo,
                    logger=self.logger,
                ),
            ),
        )
        self._turn_dispatcher = TurnDispatcher(
            TurnDispatchDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                pending_turns=principal,
                resolver=self._conversation_resolver,
                normalizer=self._inbound_turn_normalizer,
                command_executor=self._command_turn_executor,
                turn_policy=self._turn_policy,
                ingress_hook_runner=self._ingress_hook_runner,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                turn_store=self._turn_store,
                ingress=self._ingress_validator,
                visible_responses=self._visible_responses,
                retry_dispatch_sources=self._journal_dispatcher.retry_turn_sources,
                selections=self._interactive_selections,
            ),
        )
        self._coalescing_gate = CoalescingGate(
            dispatch_turn=self._turn_dispatcher.handle_prepared_turn,
            debounce_seconds=lambda: self.config.defaults.coalescing.debounce_ms / 1000,
            is_shutting_down=lambda: not self._accepting_work,
            wait_for_thread_response_idle=self._response_runner.wait_for_thread_response_idle,
            room_scope_is_single_conversation=self._conversation_resolver.room_is_one_conversation,
            active_thread_ids_for_room=self._response_runner.active_thread_ids_for_room,
            timestamp_formatter=format_local_timestamp,
            on_dispatch_failure=self._journal_dispatcher.retry_turn_sources,
            on_undelivered_source=self._journal_dispatcher.retry_turn_source,
            on_intentionally_ignored_sources=self._journal_dispatcher.settle_intentionally_ignored_turn_sources,
        )
        self._turn_ingress = TurnIngress(
            TurnIngressDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                interactive_questions=principal,
                resolver=self._conversation_resolver,
                normalizer=self._inbound_turn_normalizer,
                turn_policy=self._turn_policy,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                turn_store=self._turn_store,
                coalescing_gate=self._coalescing_gate,
                edit_regenerator=self._edit_regenerator,
                ingress=self._ingress_validator,
                visible_responses=self._visible_responses,
                dispatcher=self._turn_dispatcher,
                selections=self._interactive_selections,
                voice=self._voice_ingress,
                semantic_consumer=self._journal_dispatcher.semantic_consumer,
                claim_semantic_consumer=self._journal_dispatcher.claim_semantic_consumer,
                personal_room_command=self._personal_room_lifecycle.handle_command,
            ),
        )
        self._reaction_dispatcher = ReactionDispatcher(
            ReactionDispatcherDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                journal_dispatcher=self._journal_dispatcher,
                user_stop_reconciler=self._user_stop_reconciler,
                ingress=self._ingress_validator,
                reserve_prompt_ingress_order=self._turn_ingress.reserve_prompt_ingress_order,
                enqueue_interactive_selection=self._interactive_selections.enqueue_interactive_selection,
                hook_context=self._hook_context_support,
                resolver=self._conversation_resolver,
                config_confirmation=config_confirmation.ConfigConfirmationContext(
                    runtime=self._runtime_view,
                    runtime_paths=self.runtime_paths,
                    build_message_target=self._conversation_resolver.build_message_target,
                    delivery_gateway=self._delivery_gateway,
                ),
            ),
        )

    async def _recover_config_confirmation_setup(self, room_id: str, preview_event_id: str) -> bool:
        """Recover Matrix-backed config setup without coupling turn control to commands."""
        if self.client is None:
            msg = "Matrix client is not ready for config confirmation recovery"
            raise RuntimeError(msg)
        return await config_confirmation.recover_confirmation_setup(
            self.client,
            room_id,
            preview_event_id,
        )

    def _reply_row_resolved(self, reply_id: str) -> None:
        """Wake claims that waited for this reply's rows, and settle any debt its rows left."""
        self._reply_runtime.claim_may_proceed(reply_id)
        self._settle_reply_debt_later(reply_id)

    def _replies_ended(self, reply_ids: tuple[str, ...]) -> None:
        """Deliver what replies a settlement ended owe Matrix."""
        for reply_id in reply_ids:
            self._settle_reply_debt_later(reply_id)

    def _settle_reply_debt_later(self, reply_id: str) -> None:
        """Redact and write what a reply owes Matrix, outside the caller's task."""
        create_background_task(
            self._delivery_gateway.settle_reply_debt(reply_id),
            name=f"reply_debt_{reply_id}",
            owner=self._runtime_view,
            # Outside any span the resolving task runs: debt belongs to the reply.
            context=Context(),
        )

    @property
    def client(self) -> nio.AsyncClient | None:
        """Return the current Matrix client."""
        return self._runtime_view.client

    @client.setter
    def client(self, value: nio.AsyncClient | None) -> None:
        """Update the current Matrix client."""
        self._runtime_view.client = value

    @property
    def first_sync_complete(self) -> bool:
        """Return whether this bot generation completed its first sync."""
        return self._first_sync_done

    @property
    def config(self) -> Config:
        """Return the config the runtime published last."""
        return self._runtime_view.config

    def apply_published_config(self) -> None:
        """Refresh what this running bot derives from the config a reload just published."""
        self._personal_room_lifecycle.config_changed()
        if self._call_manager is not None:
            self._call_manager.update_config(self.config)

    @property
    def orchestrator(self) -> OrchestratorRuntime:
        """Return the runtime this bot belongs to."""
        return self._runtime_view.orchestrator

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None:
        """Return the latest event id for one Matrix thread when the projection knows it."""
        return await self._conversation_reader.latest_thread_event_id(room_id=room_id, thread_id=thread_id)

    @property
    def hook_registry(self) -> HookRegistry:
        """Return the active hook registry, which holds no hooks once this generation stopped."""
        return self._hook_registry_state.registry

    @property
    def hook_context(self) -> HookContextSupport:
        """Return the hook capabilities this bot binds into the hook contexts it emits, including its hook sender."""
        return self._hook_context_support

    @property
    def conversation_reader(self) -> ConversationReader:
        """Return the reader of this bot principal's stored conversations."""
        return self._conversation_reader

    @property
    def tool_runtime_support(self) -> ToolRuntimeSupport:
        """Return the builder of tool runtime contexts for this bot's dispatches."""
        return self._tool_runtime_support

    @property
    def ingestion_quiesced(self) -> bool:
        """Return whether shutdown or removal latched this generation's Matrix ingestion closed."""
        return self._runtime_view.ingestion_quiesced

    def admission_fence(self) -> AbstractAsyncContextManager[None]:
        """Reserve runtime-replacement admission for work done as this bot, refused once it stops accepting work."""
        return self._runtime_view.admission_fence()

    @property
    def _accepting_work(self) -> bool:
        """Return whether this generation accepts new work, which a sync or process shutdown stops."""
        return self._runtime_view.phase is RunPhase.ACCEPTING

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of active response lifecycles."""
        return self._response_runner.in_flight_response_count

    @property
    def pending_response_owner_count(self) -> int:
        """Return response tasks that still own runtime resources."""
        return self._response_runner.inbox.pending_inbox_response_count

    @property
    def pending_response_phase_counts(self) -> dict[str, int]:
        """Return non-sensitive fixed-phase counts for retained response owners."""
        return self._response_runner.inbox.pending_response_phase_counts

    @property
    def deferred_stop_required(self) -> bool:
        """Return whether response ownership deferred this bot's releases."""
        return self._deferred_stop_required

    @property
    def deferred_stop_phase(self) -> str | None:
        """Return the fixed resource-release phase, if cleanup is active."""
        phase = self._deferred_stop_phase
        return None if phase is None else phase.value

    @property
    def approval_room_ids(self) -> frozenset[str]:
        """Return configured and durably invited rooms owned by approval transport."""
        return frozenset(
            room_id for room_id in (*self.rooms, *self._room_lifecycle.invited_room_ids()) if room_id.startswith("!")
        )

    async def current_joined_room_ids(self) -> frozenset[str] | None:
        """Return the authoritative rooms this bot is currently joined to."""
        client = self.client
        if client is None or (room_ids := await get_joined_rooms(client)) is None:
            return None
        return frozenset(room_ids)

    @property
    def agent_name(self) -> str:
        """Get the agent name from username."""
        return self.agent_user.agent_name

    @cached_property
    def matrix_id(self) -> MatrixID:
        """Get the Matrix ID for this agent bot."""
        return self.agent_user.matrix_id

    def _entity_type(self) -> str:
        """Return the runtime entity type for lifecycle hooks."""
        if self.agent_name == ROUTER_AGENT_NAME:
            return "router"
        if self.agent_name in self.config.teams:
            return "team"
        return "agent"

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target currently has an active turn."""
        return self._response_runner.has_active_response_for_target(target)

    async def admit_job_follow_up(self, key: ConversationKey, follow_up_id: str) -> None:
        """Admit a follow-up for jobs that ended in one of this bot's conversations, and wake the journal worker."""
        now_ms = int(time.time() * 1000)
        await self.journal_principal().admit(
            follow_up_event(key, follow_up_id, sender_id=self.matrix_id.full_id, now_ms=now_ms),
        )
        self._journal_dispatcher.wake()

    def _build_reply_turn_owners(self, deps: ReplyTurnDeps) -> None:
        """Build one reply turn's owners so the dependency between them runs one way.

        The response runner hands paused turns to the approval runner, and both settle through the locked turns, which
        know neither.
        """
        self._cli_waiters = CliWaiters()
        lifecycle_coordinator = ResponseLifecycleCoordinator()
        inbox = InboxResponses(
            logger=self.logger,
            replies=deps.replies,
            retry_approval_sources=self.retry_approval_sources,
            runtime=deps.runtime,
        )
        locked_turns = LockedTurns(deps)
        self._approval_runner = ApprovalContinuationRunner(
            locked=locked_turns,
            lifecycle_coordinator=lifecycle_coordinator,
            cli_waiters=self._cli_waiters,
        )
        self._response_runner = ResponseRunner(
            locked=locked_turns,
            approvals=self._approval_runner,
            inbox=inbox,
            lifecycle_coordinator=lifecycle_coordinator,
            cli_waiters=self._cli_waiters,
        )

    def retry_approval_sources(self, room_id: str, source_event_ids: tuple[str, ...]) -> None:
        """Wake response-local CLI waits, then release unowned sources to the journal."""
        journal_sources = self._cli_waiters.wake(source_event_ids)
        if journal_sources:
            self._journal_dispatcher.retry_turn_sources(room_id, journal_sources)

    async def _emit_agent_lifecycle_event(
        self,
        event_name: str,
        *,
        stop_reason: str | None = None,
    ) -> None:
        """Emit one agent lifecycle observer event for this bot."""
        if not self.hook_registry.has_hooks(event_name):
            return

        configured_rooms = tuple(get_rooms_for_entity(self.agent_name, self.config))
        joined_room_ids = tuple(room_id for room_id in self.rooms if room_id.startswith("!"))
        if event_name == EVENT_BOT_READY and self.client is not None:
            joined_room_ids = tuple(
                dict.fromkeys(room_id for room_id in (*self.rooms, *self.client.rooms) if room_id.startswith("!")),
            )
        context = AgentLifecycleContext(
            **self._hook_context_support.base_kwargs(event_name, f"{event_name}:{self.agent_name}:{uuid4().hex}"),
            entity_name=self.agent_name,
            entity_type=self._entity_type(),
            rooms=configured_rooms,
            matrix_user_id=self.agent_user.user_id,
            joined_room_ids=joined_room_ids,
            stop_reason=stop_reason,
        )
        await emit(self.hook_registry, event_name, context)

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        await self._room_lifecycle.join_configured_rooms()

    async def _post_join_room_setup(self, room_id: str) -> None:
        """Run room setup that should happen after configured joins and across restarts."""
        self._request_call_reconciliation(room_id)
        if self._router is not None:
            await self._router.room_joined(room_id)

    async def _restore_scheduled_tasks(self, room_id: str) -> None:
        """Hand the schedules this bot owns in a room it joined to the runtime's scheduled-task owner."""
        await self.orchestrator.scheduled_tasks.restore_room(self, room_id)

    async def change_local_membership(
        self,
        room_id: str,
        target_membership: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> bool:
        """Serialize one local membership command while ingestion keeps draining.

        Every join, including configured, invited, and personal rooms and the rejoin at startup, publishes the entity's
        MindRoom server in the room, so MindRoom Chat can find it.
        """
        session = self._ingestion_session
        if session is None:
            message = "owned Matrix ingestion session is missing"
            raise PermanentMatrixStartupError(message)
        async with self._local_membership_lock:
            changed = await change_local_membership(
                session,
                account_id=self.agent_user.user_id,
                room_id=room_id,
                target_membership=target_membership,
                read_position=self.journal_principal().ingestion_membership_position,
                admission_progress=self._ingestion_admission_progress,
                subscribe_awaited_rooms=lambda: subscribe_sliding_rooms(session, self.config, self.rooms),
                is_authorized=is_authorized,
            )
        if changed and target_membership == "join" and self.client is not None:
            content = server_state_content(
                self.agent_name,
                self.config,
                self.runtime_paths,
                api_enabled=self.orchestrator.api_enabled,
            )
            await publish_server_state(self.client, room_id, content)
        return changed

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for."""
        await self._room_lifecycle.leave_unconfigured_rooms()

    async def refresh_presence(self) -> None:
        """Set presence status with model information."""
        if self.client is None:
            return

        status_msg = build_agent_status_message(
            self.agent_name,
            self.config,
            config_plugins(self.config, self.runtime_paths).tools,
            voice_calls_available=(self._call_manager is not None and self._call_manager.voice_backend_available),
        )
        await set_presence_status(self.client, status_msg)

    def mark_sync_loop_started(self) -> None:
        """Record that a sync loop iteration is starting.

        Does NOT arm the monotonic watchdog clock — that only starts when the
        first ``SyncResponse`` or ``SyncError`` arrives.  The watchdog has its
        own startup timeout for the pre-first-response window.
        """
        if not self._runtime_view.resume():
            return
        self._sync_shutdown_budget = None
        self._deferred_stop_required = False
        self._deferred_stop_phase = None
        self._request_call_reconciliation()
        self.invalidate_agent_reply_memberships(reason="sync_loop_started")
        mark_matrix_sync_loop_started(self.agent_name)

    def invalidate_agent_reply_memberships(self, *, reason: str) -> None:
        """Fail room-backed reply grants closed when the router's sync can no longer vouch for them."""
        if self._router is not None:
            self._router.invalidate_memberships(reason=reason)

    @property
    def active_call_requesters(self) -> tuple[str, ...]:
        """Return the requester of each voice call this bot has joined or is joining."""
        call_manager = self._call_manager
        return () if call_manager is None else call_manager.active_call_requesters

    def schedule_pending_invite_reconciliation(self) -> None:
        """Recheck cached invites in the background while this bot accepts work, so its own drain covers the task."""
        if not self._accepting_work or self.client is None:
            return
        create_background_task(
            self._room_lifecycle.reconcile_pending_invites(),
            name=f"pending_invite_reconciliation_{self.agent_name}",
            owner=self._runtime_view,
        )

    async def revoke_reply_authorized_calls(self) -> None:
        """End active calls that no longer pass current reply access."""
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.revoke_reply_authorization()

    def schedule_reply_authorized_call_reconciliation(self) -> None:
        """Recheck this bot's calls in the background while it accepts work, starting the ones that became allowed."""
        call_manager = self._call_manager
        if call_manager is None or not self._accepting_work:
            return
        create_background_task(
            call_manager.reconcile_reply_authorization(),
            name=f"matrix_rtc_reply_authorization_{self.agent_name}",
            owner=self._runtime_view,
        )

    def schedule_reply_authorized_call_revocation(self) -> None:
        """End this bot's denied calls in the background, in every phase, since revocation is cleanup."""
        call_manager = self._call_manager
        if call_manager is None:
            return
        create_background_task(
            call_manager.revoke_reply_authorization(),
            name=f"matrix_rtc_reply_authorization_revoke_{self.agent_name}",
            owner=self._runtime_view,
        )

    def reset_watchdog_clock(self) -> None:
        """Reset the monotonic watchdog clock for a fresh sync iteration."""
        self._last_sync_monotonic = None

    def seconds_since_last_sync_activity(self) -> float | None:
        """Return elapsed seconds since the last sync-loop activity seen by the watchdog."""
        if self._last_sync_monotonic is None:
            return None
        return time.monotonic() - self._last_sync_monotonic

    def durable_ingestion_progress_generation(self) -> int | None:
        """Return nio's commit-gated progress generation for the owned session."""
        session = self._ingestion_session
        return session.progress_generation if session is not None else None

    def _mark_sync_progress(self) -> None:
        """Advance watchdog and health freshness from one sync progress event."""
        self.last_sync_time = mark_matrix_sync_success(self.agent_name)
        self._last_sync_monotonic = time.monotonic()

    async def _recover_unacknowledged_matrix_deliveries(self) -> bool:
        """Resend answers this bot could not confirm reaching Matrix.

        After the first sync response, not before it. A send into an encrypted
        room is refused until sync has rebuilt the local room and device state,
        so recovery run at startup would spend its retry budget failing and
        leave every encrypted room's owed answer unsent -- exactly the rooms
        this matters in.

        The first response is not always enough, though. It can arrive while a
        room is still unrecovered, and nio refuses ordinary sends into one. So
        the pass runs after every sync response, not once.

        It asks the outbox rather than tracking whether anything is owed. A
        flag would have to be armed from every path that can leave a row
        unacknowledged -- a pass that raised before reporting, a live send the
        homeserver refused, a claim that never came back -- and the answer a
        user is waiting for would be lost by whichever path nobody remembered.
        The outbox already knows, the query is one probe of a partial index,
        and it sends nothing when nothing is owed.

        Resending is safe: each delivery carries the transaction ID its first
        attempt used, so one the homeserver already accepted collapses back
        onto the same event.
        """
        try:
            # A deletion history recovery learned ended replies too: their spans stop and what they owe follows.
            self._replies_ended(await self._reply_runtime.deletions_ended())
            outcome = await self._delivery_gateway.recover_deliveries()
        except Exception:
            self.logger.exception("Delivery recovery failed")
            return False
        if outcome.recovered:
            self.logger.info("Resent unacknowledged deliveries", deliveries=outcome.recovered)
        if not outcome.complete:
            self.logger.warning("Deliveries still unsent after recovery", deliveries=outcome.failed)
        return outcome.complete

    def _schedule_delivery_recovery(self) -> None:
        """Wake this bot's one outbox recovery task after sync progress."""
        if not self._accepting_work:
            return
        self._delivery_recovery_wake.set()
        task = self._delivery_recovery_task
        if task is not None and not task.done():
            return
        self._delivery_recovery_task = create_background_task(
            self._run_scheduled_delivery_recovery(),
            name=f"delivery_recovery_{self.agent_name}",
            owner=self._runtime_view,
            # Recovery outlives the sync request generation, so it must not inherit that context.
            context=Context(),
        )

    async def _wait_for_delivery_projection(self) -> None:
        """Retain the pump until projection advances or its supervisor cancels it.

        A final recovery pass can still unblock captured input during shutdown.
        Otherwise bounded source quiescence expires before supervisor teardown
        cancels the pump; cancelling here would also abort the source it drains.
        """
        task = self._delivery_recovery_task
        if task is None or task.done():
            self._schedule_delivery_recovery()
        await self._delivery_projection_progress.wait()
        self._delivery_projection_progress.clear()

    async def _run_scheduled_delivery_recovery(self) -> None:
        """Recover outbox debt without making Matrix receive progress wait."""
        retry_delay = _DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS
        try:
            while self._accepting_work:
                self._delivery_recovery_wake.clear()
                complete = await self._recover_unacknowledged_matrix_deliveries()
                self._delivery_projection_progress.set()
                if complete:
                    retry_delay = _DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS
                    if not self._delivery_recovery_wake.is_set():
                        return
                    continue
                if self._delivery_recovery_wake.is_set():
                    continue
                try:
                    await asyncio.wait_for(self._delivery_recovery_wake.wait(), timeout=retry_delay)
                except TimeoutError:
                    retry_delay = min(
                        retry_delay * 2,
                        _DELIVERY_RECOVERY_RETRY_MAX_DELAY_SECONDS,
                    )
        finally:
            self._delivery_projection_progress.set()
            if self._delivery_recovery_task is asyncio.current_task():
                self._delivery_recovery_task = None

    async def _run_sync_response_side_effects(
        self,
        *,
        first_sync_response: bool,
    ) -> None:
        """Run side effects that do not own raw sync checkpoint safety."""
        if self._router is not None:
            await self._router.refresh_memberships_if_needed()
        self._schedule_delivery_recovery()
        self._schedule_handled_turn_cleanup()
        if first_sync_response:
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)
        self._personal_room_lifecycle.schedule_reconciliation()

        if not self._orchestrator_ready_handled:
            await self.orchestrator.handle_bot_ready(self)
            self._orchestrator_ready_handled = True

    def _before_ingestion_admission(
        self,
        admission: IngestionRecordAdmission,
        timeline_provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        """Revoke uncertain control-room grants before admitting more work."""
        if self._router is not None:
            self._router.before_ingestion_admission(admission, timeline_provenance)

    async def _after_ingestion_admission(
        self,
        admission: IngestionRecordAdmission,
        facts: AdmissionFacts,
        timeline_provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        """Apply room effects after their source is durable, retrying unfinished effects."""
        if admission.membership is not None:
            await self._apply_ingestion_membership(admission)
        event = admission.event
        if event is None:
            return
        live_member = event.kind is EventKind.ROOM_LIFECYCLE and timeline_provenance is nio.TimelineEventProvenance.LIVE
        member_activity = event.kind is EventKind.ROOM_LIFECYCLE and timeline_provenance in {
            nio.TimelineEventProvenance.LIVE,
            nio.TimelineEventProvenance.RECOVERED,
        }
        if facts.receipt_new and (admission.projected is not None or member_activity):
            self.orchestrator.observe_room_activity(event.room_id)
        if live_member and self._router is not None:
            parsed = nio.RoomMemberEvent.from_dict(dict(event.source))
            assert isinstance(parsed, nio.RoomMemberEvent)
            await self._router.live_membership_transition(event.room_id, parsed)

    async def _apply_ingestion_membership(self, admission: IngestionRecordAdmission) -> None:
        """Reconcile app state only while this admitted membership is still current."""
        room_id = admission.room_id
        assert room_id is not None
        position = await self.journal_principal().ingestion_membership_position(room_id)
        assert position is not None
        joined = admission.membership == "join"
        expected_membership = "join" if joined else "leave"
        if (position.membership, position.membership_epoch) != (expected_membership, admission.membership_epoch):
            return
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_sync_room_membership(
                joined_room_ids={room_id} if joined else set(),
                left_room_ids=set() if joined else {room_id},
            )
            if joined:
                self._request_call_reconciliation(room_id)
        if not joined and admission.previous_membership == "join":
            await self._reply_runtime.departed(room_id)
            if (runtime := get_background_runtime(self.runtime_paths)) is not None:
                # Nobody here can see or cancel the work this entity leaves behind.
                await cancel_room_jobs(runtime, agent_name=self.agent_name, room_id=room_id)
            await self._room_lifecycle.forget_invited_room(room_id)

    async def ensure_rooms(self) -> None:
        """Ensure agent is in the correct rooms based on configuration.

        This consolidates room management into a single method that:
        1. Joins configured rooms
        2. Leaves unconfigured rooms
        """
        await self.join_configured_rooms()
        await self.leave_unconfigured_rooms()
        if self._ingestion_session is not None:
            await subscribe_sliding_rooms(self._ingestion_session, self.config, self.rooms)

    async def _register_call_manager_callbacks(self, client: nio.AsyncClient) -> None:
        """Build the optional call manager and wire its Matrix callbacks."""
        self._call_manager = await maybe_build_call_manager(
            agent_name=self.agent_name,
            config=self.config,
            client=client,
            runtime_paths=self.runtime_paths,
            ssl_verify=constants.runtime_matrix_ssl_verify(self.runtime_paths),
            tool_support=self._tool_runtime_support,
            get_invited_rooms_by_agent=self._invited_call_rooms_by_agent,
            agent_reply_memberships=self._runtime_view.agent_reply_memberships,
            response_admission_gate=self._runtime_view.response_admission_gate,
            admission_fence=self._runtime_view.admission_fence,
        )

        call_manager = self._call_manager
        if call_manager is None:
            return

        client.add_to_device_callback(
            _create_best_effort_task_wrapper(
                call_manager.on_to_device_event,
                owner=self._runtime_view,
            ),
            AuthenticatedToDeviceEvent,
        )

    def _invited_call_rooms_by_agent(self) -> dict[str, frozenset[str]]:
        """Return the accepted-invite rooms of each configured call agent the runtime runs."""
        config = self.config
        fleet = self.orchestrator.agent_bots
        return {
            agent_name: read_invited_rooms(self.runtime_paths, config, agent_name)
            for agent_name in config.calls.agents
            if agent_name in fleet
        }

    async def _on_rtc_event(
        self,
        room: nio.MatrixRoom,
        event: nio.UnknownEvent,
    ) -> None:
        """Apply an admitted RTC event through the current call runtime."""
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_room_event(room, event)

    async def _on_ingestion_frame_completion(self) -> None:
        """Publish one fully settled source frame to the bot runtime."""
        first_sync_response = not self._first_sync_done
        self._mark_sync_progress()
        if not self._accepting_work:
            return
        client = self.client
        if client is not None:
            await self._room_lifecycle.observe_trusted_sync_rooms(client.rooms)
        self._first_sync_done = True
        await self._run_sync_response_side_effects(
            first_sync_response=first_sync_response,
        )
        call_manager = self._call_manager
        # Draining can begin during the awaits above; pending call work then waits for the next accepting frame.
        if (
            call_manager is None
            or not self._accepting_work
            or (not self._calls_full_reconcile_pending and not self._call_rooms_reconcile_pending)
        ):
            return
        reconcile_all = self._calls_full_reconcile_pending
        room_ids = frozenset(self._call_rooms_reconcile_pending)
        self._calls_full_reconcile_pending = False
        self._call_rooms_reconcile_pending.clear()
        reconcile = (
            call_manager.reconcile_joined_rooms() if reconcile_all else call_manager.reconcile_joined_rooms(room_ids)
        )
        create_background_task(
            reconcile,
            name=f"matrix_rtc_reconcile_{self.agent_name}",
            owner=self._runtime_view,
        )

    def _request_call_reconciliation(self, room_id: str | None = None) -> None:
        """Retain full or room-scoped reconciliation intent until frame publication."""
        if self._call_manager is None:
            return
        if room_id is None:
            self._calls_full_reconcile_pending = True
            self._call_rooms_reconcile_pending.clear()
        elif not self._calls_full_reconcile_pending:
            self._call_rooms_reconcile_pending.add(room_id)

    async def _open_owned_matrix_client(self) -> nio.AsyncClient:
        """Acquire and retain the bot's exclusive ingestion/client ownership."""
        opened = await login_agent_owned_session(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
            consumer_store=self.journal_principal(),
            new_consumer_generation=uuid4(),
            config=bot_ingestion_config(
                self.config,
                agent_name=self.agent_name,
                room_ids=self.rooms,
                timeout_ms=_SYNC_TIMEOUT_MS,
                sync_filter=_SYNC_FILTER,
            ),
        )
        self.client = opened.client
        self._ingestion_session = opened.session
        return opened.client

    async def _close_owned_matrix_after_start_failure(self) -> None:
        """Release both Matrix ownership lanes without masking startup failure."""
        ingestion_session = self._ingestion_session
        client = self.client
        self.running = False
        self._ingestion_session = None
        self.client = None
        if ingestion_session is not None:
            try:
                await ingestion_session.close()
            except BaseException:
                self.logger.warning("Failed to close ingestion session after startup failure", exc_info=True)
        if client is not None:
            try:
                await client.close()
            except BaseException:
                self.logger.warning("Failed to close Matrix client after startup failure", exc_info=True)

    async def _take_over_replies(self) -> None:
        """Own this principal's replies, then hold the conversation each of its pending approvals waits in.

        Both happen before journal replay, so a message to a held conversation waits for its approval after a restart.
        """
        await self._reply_runtime.start()
        self._response_runner.follow_approval_holds(self._journal_store.approval_signals, self._journal_principal_id)

    async def start(self) -> None:
        """Log in with the account the runtime prepared and start this bot's services, without joining rooms yet."""
        client = await self._open_owned_matrix_client()
        # Captured the moment it becomes true. A restart that logs in as a new
        # device must not resend under the old device's transaction IDs
        # believing they still deduplicate.
        self._sending_device_id = client.device_id or None
        try:
            self.orchestrator.validate_managed_entity_identities()
            self._runtime_view.mark_runtime_started()
            await self._room_lifecycle.restore_pending_join_decrypt_fences()
            await ensure_entity_avatar(client, self.agent_name, self.config, self.runtime_paths, logger=self.logger)
            # Keep durable tracking-state loading off the event loop at startup.
            await self._turn_store.warm()
            # This bot instance now owns its replies; spans of earlier instances can no longer write.
            await self._take_over_replies()
            client = self.client
            assert client is not None

            # Persist correctness-critical source events; keep ID-less auxiliary inputs best-effort.
            client.add_event_callback(
                self._room_lifecycle.on_invite,  # ty: ignore[invalid-argument-type]
                nio.InviteEvent,  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
            )
            await self._register_call_manager_callbacks(client)
            register_desktop_pairing_receiver(
                self.config,
                client=client,
                agent_name=self.agent_name,
                runtime_paths=self.runtime_paths,
                callback_wrapper=partial(_create_best_effort_task_wrapper, owner=self._runtime_view),
            )
            register_model_catalog_receiver(
                client=client,
                agent_name=self.agent_name,
                runtime_paths=self.runtime_paths,
                config_getter=lambda: self.config,
                membership_index=self._runtime_view.agent_reply_memberships,
                callback_wrapper=partial(_create_best_effort_task_wrapper, owner=self._runtime_view),
            )
            await self.refresh_presence()
            self.running = True

            if self._router is not None:
                await self._router.started(client)

            # Note: Room joining is deferred until after invitations are handled
            self.logger.info("agent_setup_complete", user_id=self.agent_user.user_id)
            await self._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)
        except BaseException:
            await self._close_owned_matrix_after_start_failure()
            raise

    async def _open_approval_recovery_client(self) -> None:
        """Open only the original Matrix sender needed to recover a frozen FINAL."""
        if self.client is not None:
            return
        client = await self._open_owned_matrix_client()
        self._sending_device_id = client.device_id or None
        try:
            self._runtime_view.mark_runtime_started()
            await self._turn_store.warm()
        except BaseException:
            self._sending_device_id = None
            await self._close_owned_matrix_after_start_failure()
            raise

    async def recover_approval_final(self, approval_id: str) -> bool:
        """Recover one frozen approval answer, owning any recovery-only client lifetime."""
        opened_recovery_client = self.client is None
        try:
            if opened_recovery_client:
                await self._open_approval_recovery_client()
                # A recovery-only bot is its own instance for the replies it finishes.
                await self._reply_runtime.take_ownership()
            return await self._approval_runner.recover_approval_final(approval_id)
        finally:
            if opened_recovery_client:
                await self._close_approval_recovery_client()

    async def _close_approval_recovery_client(self) -> None:
        """Close a recovery-only Matrix client without starting normal bot services."""
        client = self.client
        if client is not None:
            await wait_for_background_tasks(timeout=5.0, owner=self._runtime_view)
        self._sending_device_id = None
        await self._close_owned_matrix_after_start_failure()

    def release_pending_turn_journal_replay(self) -> None:
        """Start semantic dispatch after the runtime publishes initial memberships."""
        if not self._accepting_work:
            return
        self._journal_dispatcher.start()
        self._journal_dispatcher.release_turn_replay()

    async def recover_pending_turn_journal_events(self) -> None:
        """Drain fleet-dependent turn replay after the responder startup pass."""
        self.release_pending_turn_journal_replay()
        await self._journal_dispatcher.drain_once()
        await self._cleanup_handled_turns()

    async def _cleanup_handled_turns(self) -> None:
        """Apply handled-turn retention to every source dispatch no longer owns."""
        await self._turn_store.cleanup(
            unsettled_source_event_ids=await self._journal_dispatcher.unsettled_event_ids(),
        )
        await self._reply_runtime.forget_finished()

    def _schedule_handled_turn_cleanup(self) -> None:
        """Start one retention pass per interval so records do not wait for a restart."""
        now = time.monotonic()
        if not self._accepting_work or now < self._next_handled_turn_cleanup_at:
            return
        self._next_handled_turn_cleanup_at = now + _HANDLED_TURN_CLEANUP_INTERVAL_SECONDS
        create_background_task(
            self._cleanup_handled_turns(),
            name=f"handled_turn_cleanup_{self.agent_name}",
            owner=self._runtime_view,
            # The pass covers every room's records, so it must not inherit the sync frame's context.
            context=Context(),
        )

    async def leave_rooms(self) -> None:
        """Leave rooms for entity removal while the orchestrator keeps sync alive."""
        await self._room_lifecycle.leave_all_rooms(timeout_seconds=SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS)

    async def stop(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        """Stop the agent bot."""
        if shutdown_intent.stop_reason == "shutdown":
            self.begin_process_shutdown()
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._orchestrator_ready_handled = False
        clear_matrix_sync_state(self.agent_name)
        await self._emit_agent_lifecycle_event(EVENT_AGENT_STOPPED, stop_reason=shutdown_intent.stop_reason)
        self._hook_registry_state.end()

        call_manager = self._call_manager
        self._call_manager = None
        self._calls_full_reconcile_pending = False
        self._call_rooms_reconcile_pending = set()
        if call_manager is not None:
            await call_manager.shutdown()

        failures: list[BaseException] = []
        await self._release(
            "sync shutdown preparation",
            self.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent),
            failures,
            defer_expected_response_timeout=shutdown_intent.stop_reason == "shutdown",
        )
        pending_response_count = self._response_runner.inbox.pending_inbox_response_count
        pending_callback_count = (
            self._journal_dispatcher.pending_task_count if shutdown_intent.stop_reason == "shutdown" else 0
        )
        if shutdown_intent.stop_reason != "restart" and (pending_response_count > 0 or pending_callback_count > 0):
            self._deferred_stop_required = True
            if failures:
                raise failures[0]
            msg = (
                f"{pending_response_count} response tasks and {pending_callback_count} journal tasks "
                "still own runtime resources"
            )
            raise ResponseShutdownTimeoutError(msg)

        await self._release_stopped_resources(failures, shutdown_intent=shutdown_intent)
        if failures:
            # Every step ran, and the caller still has to hear about it.
            # The fleet drops a stopped slot only after every stop phase
            # succeeded, so raising keeps this bot registered and stops the reload
            # before it creates a replacement on a store that never closed.
            # Swallowing is what would certify a partial stop as a clean one.
            # A restart still replaces a bot whose only failure is a reply that
            # outlived the drain, so a failed release must win over that timeout.
            raise next(
                (failure for failure in failures if not isinstance(failure, ResponseShutdownTimeoutError)),
                failures[0],
            )
        self._deferred_stop_required = False
        self.logger.info("Stopped agent bot")

    async def finish_deferred_stop(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
        timeout_seconds: float = RESPONSE_FINALIZATION_TIMEOUT_SECONDS,
    ) -> None:
        """Finish releases deferred while a response still owned the runtime."""
        if shutdown_intent.stop_reason != "shutdown":
            msg = "deferred response finalization requires orderly process shutdown"
            raise RuntimeError(msg)
        if not self._deferred_stop_required:
            return
        shutdown_budget = ShutdownBudget.start(timeout_seconds)
        try:
            self._deferred_stop_phase = DeferredStopPhase.JOURNAL_DISPATCHER
            if not await self._journal_dispatcher.wait_stopped(timeout_seconds=shutdown_budget.remaining_seconds()):
                msg = "journal callback cleanup exceeded bounded finalization"
                raise ResponseShutdownTimeoutError(msg)
            self._deferred_stop_phase = DeferredStopPhase.RECOVERY_PROOF
            recoverable = await self._response_runner.inbox.finish_process_shutdown_recovery(
                timeout_seconds=shutdown_budget.remaining_seconds(),
            )
            if not recoverable:
                msg = "deferred response cleanup lacks durable recovery proof"
                raise ResponseShutdownTimeoutError(msg)
            failures: list[BaseException] = []
            await self._release_stopped_resources(failures, shutdown_intent=shutdown_intent)
            if failures:
                raise failures[0]
            self._deferred_stop_required = False
            self.logger.info("Stopped agent bot after deferred response cleanup")
        finally:
            self._deferred_stop_phase = None

    async def _release_stopped_resources(
        self,
        failures: list[BaseException],
        *,
        shutdown_intent: RuntimeShutdownIntent,
    ) -> None:
        """Release resources after every response owner is terminal."""
        try:
            # Its replacement follows this principal's approvals from here on.
            self._response_runner.stop_following_approval_holds()
            # Each of these owns a resource this bot alone holds, and none of them
            # may skip the others: a lane that already faulted makes the dispatcher's
            # stop raise, which must not leave the ingestion session or the client
            # open, nor abort a config reload's removal of this generation.
            self._mark_deferred_stop_phase(DeferredStopPhase.JOURNAL_DISPATCHER)
            await self._release(
                "journal dispatcher",
                self._journal_dispatcher.stop(shutdown_intent=shutdown_intent),
                failures,
            )
            if shutdown_intent.stop_reason == "restart":
                await self._response_runner.inbox.wait_for_source_owned_inbox_responses()
            if self._ingestion_session is not None:
                self._mark_deferred_stop_phase(DeferredStopPhase.INGESTION_SESSION)
                await self._release("ingestion session", self._ingestion_session.close(), failures)
            if self.client is not None:
                self._mark_deferred_stop_phase(DeferredStopPhase.MATRIX_CLIENT)
                self.logger.warning("Client is not None in stop()")
                await self._release("matrix client", self.client.close(), failures)
        finally:
            self._deferred_stop_phase = None

    def _mark_deferred_stop_phase(self, phase: DeferredStopPhase) -> None:
        """Expose one fixed release phase while any stop cleanup owns it."""
        self._deferred_stop_phase = phase

    async def _release(
        self,
        what: str,
        closing: Awaitable[None],
        failures: list[BaseException],
        *,
        defer_expected_response_timeout: bool = False,
    ) -> None:
        """Await one shutdown step, recording failure so the later steps still run."""
        try:
            await closing
        except BaseException as error:
            if defer_expected_response_timeout and isinstance(
                error,
                ResponseShutdownTimeoutError,
            ):
                self.logger.warning(
                    "Deferred resource release after bounded response shutdown",
                    error_type=error.__class__.__name__,
                    resource=what,
                )
            else:
                self.logger.exception(
                    "Failed to release resource during stop",
                    resource=what,
                )
            failures.append(error)

    def begin_process_shutdown(self) -> None:
        """Close response and callback admission before any shutdown teardown yields."""
        self._runtime_view.begin_process_shutdown()
        self._delivery_recovery_wake.set()
        self._journal_dispatcher.begin_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)
        self._response_runner.inbox.begin_process_shutdown()

    async def prepare_for_sync_shutdown(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        """Cancel work that must not outlive the Matrix sync loop."""
        if self._accepting_work:
            self.logger.info(
                "matrix_agent_response_runtime_shutdown",
                active_response_count=self.in_flight_response_count,
                restart_reason_category=restart_reason_category_for(shutdown_intent),
                resulting_action="drain_then_cancel_response_runtime",
            )
        self._runtime_view.begin_draining()
        self._delivery_recovery_wake.set()
        if shutdown_intent.stop_reason == "shutdown":
            self.begin_process_shutdown()
            if self.client is not None:
                cast(
                    _ProcessShutdownMatrixClient,  # noqa: TC006 - runtime reference proves the private protocol is live
                    self.client,
                ).begin_process_shutdown_transport_fence()
        shutdown_budget = self._sync_shutdown_budget
        if shutdown_budget is None:
            shutdown_budget = ShutdownBudget.start(SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS)
            self._sync_shutdown_budget = shutdown_budget
        await self._personal_room_lifecycle.cancel_reconciliation(timeout_seconds=shutdown_budget.remaining_seconds())
        background_tasks_completed = await wait_for_background_tasks(
            timeout=shutdown_budget.remaining_seconds(),
            owner=self._runtime_view,
            shutdown_intent=shutdown_intent,
        )
        callbacks_drained = shutdown_intent.stop_reason != "shutdown" or await self._journal_dispatcher.wait_stopped(
            timeout_seconds=shutdown_budget.remaining_seconds(),
        )
        drain_result = await self._coalescing_gate.drain_all(
            shutdown_budget=shutdown_budget,
            shutdown_intent=shutdown_intent,
        )
        if drain_result.admission_deferred_count:
            self.logger.info(
                "coalescing_admission_deferred_for_durable_recovery",
                agent_name=self.agent_name,
                admission_deferred_count=drain_result.admission_deferred_count,
            )
        responses_drained = await self._response_runner.inbox.drain_inbox_responses(
            cancel_after_seconds=shutdown_budget.per_window_seconds(windows=2),
            shutdown_intent=shutdown_intent,
        )
        pending_response_count = self._response_runner.inbox.pending_inbox_response_count
        if not responses_drained:
            self.logger.warning(
                "matrix_agent_response_drain_incomplete",
                agent_name=self.agent_name,
                active_response_count=self.in_flight_response_count,
                pending_response_count=pending_response_count,
                response_recovery_complete=self._response_runner.inbox.incomplete_inbox_responses_recoverable,
                restart_reason_category=restart_reason_category_for(shutdown_intent),
            )
        post_drain_background_tasks_completed = await wait_for_background_tasks(
            timeout=shutdown_budget.remaining_seconds(),
            owner=self._runtime_view,
            shutdown_intent=shutdown_intent,
        )
        if (
            not background_tasks_completed
            or not callbacks_drained
            or not drain_result.completed
            or not responses_drained
            or not post_drain_background_tasks_completed
        ):
            self.logger.warning(
                "runtime_drain_incomplete_with_durable_dispatch_recovery",
                agent_name=self.agent_name,
                background_tasks_completed=background_tasks_completed,
                callbacks_drained=callbacks_drained,
                coalescing_drain_completed=drain_result.completed,
                responses_drained=responses_drained,
                response_recovery_complete=self._response_runner.inbox.incomplete_inbox_responses_recoverable,
                post_drain_background_tasks_completed=post_drain_background_tasks_completed,
                released_reservation_count=drain_result.released_reservation_count,
                cancelled_unready_count=drain_result.cancelled_unready_count,
                failed_ready_count=drain_result.failed_ready_count,
                dropped_ready_count=drain_result.dropped_ready_count,
                dispatch_failure_count=drain_result.dispatch_failure_count,
                dispatch_cancelled_count=drain_result.dispatch_cancelled_count,
            )

    async def quiesce_matrix_ingestion(self) -> None:
        """Bound source drain; unacknowledged input survives later cancellation."""
        self._runtime_view.quiesce_ingestion()
        session = self._ingestion_session
        if session is not None:
            try:
                async with asyncio.timeout(SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS):
                    await session.quiesce()
            except TimeoutError:
                self.logger.warning("matrix_ingestion_quiesce_timeout")

    async def sync_forever(self) -> None:
        """Run the owned durable source and batch admission pump together."""
        client = self.client
        session = self._ingestion_session
        assert client is not None
        if session is None:
            message = "owned Matrix ingestion session is missing"
            raise PermanentMatrixStartupError(message)
        device_id = client.device_id
        if type(device_id) is not str or not device_id:
            message = "owned Matrix device ID is missing"
            raise PermanentMatrixStartupError(message)

        runner = asyncio.create_task(
            session.run(),
            name=f"matrix_ingestion_runner_{self.agent_name}",
        )
        pump = asyncio.create_task(
            run_ingestion_pump(
                session,
                self.journal_principal(),
                account_id=self.matrix_id.full_id,
                wait_for_work=session.wait_for_work,
                wake_semantic_dispatch=self._journal_dispatcher.wake,
                wait_for_delivery_projection=self._wait_for_delivery_projection,
                before_admission=self._before_ingestion_admission,
                after_admission=self._after_ingestion_admission,
                after_sync=self._on_ingestion_frame_completion,
                after_ack=self._ingestion_admission_progress.set,
                on_decryption_failure=self._decryption_diagnostics.schedule,
                schedule_trigger_sender_is_managed=self._ingress_validator.sender_is_trusted_for_ingress_metadata,
            ),
            name=f"matrix_ingestion_pump_{self.agent_name}",
        )
        tasks = (runner, pump)
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in tasks:
                if task in done:
                    task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _room_for_journal_event(self, room_id: str) -> nio.MatrixRoom:
        """Resolve one recovery room without depending on a new sync response."""
        client = self.client
        if client is not None and room_id in client.rooms:
            return client.rooms[room_id]
        return nio.MatrixRoom(room_id, self.matrix_id.full_id)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        """Delegate one inbound text event to the turn engine.

        Every `m.room.message` the journal admitted as work reaches here, which
        is `m.text` and `m.emote`. An emote is a user utterance whose body is
        written in the third person, so it needs no special handling: the same
        mentions, commands, and routing apply to `/me asks the bot to X` as to
        the sentence typed without the `/me`.
        """
        return await self._turn_ingress.handle_text_event(room, event)

    async def _on_redaction(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:  # noqa: ARG002
        """Run what the redaction's admission left for the bot.

        Admission recorded the redaction for the agent, removed its text, and
        ended the replies that lost every source with it, all in one
        transaction, so this owes only those replies' effects: their running
        spans stop and what they showed is redacted. Raising leaves the callback
        unaccepted and the source available for sync to redeliver.
        """
        self._replies_ended(await self._reply_runtime.deletions_ended())

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> TurnDispatchOutcome:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_lookup_scope():
            return await self._reaction_dispatcher.dispatch(room, event)

    async def _on_room_member(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMemberEvent,
    ) -> None:
        """Forward one live room member event to the call manager, the personal-room lifecycle, and the router's hooks."""
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_room_membership_event(room, event)
        await self._personal_room_lifecycle.member_event(room, event)
        if self._router is not None:
            await self._router.member_event(room, event)

    def _lookup_personal_room_target(self, agent_name: str) -> PersonalRoomTarget | None:
        """Project a connected fleet member into its service and sync readiness."""
        target = self.orchestrator.agent_bots.get(agent_name)
        if target is None or target.client is None:
            return None
        return PersonalRoomTarget(target.personal_rooms, target.first_sync_complete)

    def personal_room_target_ready(self) -> None:
        """Let the router onboard a user at once because their own agent finished its first sync."""
        self._personal_room_lifecycle.target_ready()

    def _report_personal_room_membership_change(self, room_id: str, event_id: str) -> None:
        """Forward a room's membership event to the router, which owns personal-room retries."""
        router = self.orchestrator.agent_bots.get(ROUTER_AGENT_NAME)
        if router is not None:
            router.personal_room_membership_changed(room_id, event_id)

    def personal_room_membership_changed(self, room_id: str, event_id: str) -> None:
        """Retry at once onboarding that the personal room's imported roster blocked, as its membership changed."""
        self._personal_room_lifecycle.membership_changed(room_id, event_id)

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
    ) -> TurnDispatchOutcome:
        """Delegate one inbound media event to the turn engine."""
        return await self._turn_ingress.handle_media_event(room, event)
