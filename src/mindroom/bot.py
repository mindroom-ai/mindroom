"""Matrix runtime shell for agents, teams, and the router."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import cached_property, partial
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import nio
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.approval_inbound import (
    handle_tool_approval_action,
    maybe_handle_tool_approval_reply,
    parse_approval_response_event,
)
from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.desktop.pairing_receiver import register_desktop_pairing_receiver
from mindroom.entity_resolution import entity_identity_registry
from mindroom.handled_turns import legacy_responses_file_path
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    EVENT_REACTION_RECEIVED,
    EVENT_ROOM_MEMBER_JOINED,
    AgentLifecycleContext,
    HookContextSupport,
    HookRegistry,
    HookRegistryState,
    ReactionReceivedContext,
    RoomMemberJoinedContext,
    emit,
    send_hook_message,
)
from mindroom.matrix.decrypt_failure import handle_decrypt_failure
from mindroom.matrix.event_info import EventInfo, origin_server_ts_from_event_source
from mindroom.matrix.health import (
    SyncCacheWriteProgress,
    clear_matrix_sync_state,
    get_matrix_sync_cache_write_progress,
    mark_matrix_sync_loop_started,
    mark_matrix_sync_success,
    track_matrix_sync_cache_write,
)
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import leave_non_dm_rooms
from mindroom.matrix.state import resolve_room_aliases
from mindroom.matrix.sync_certification import (
    SyncCertificationDecision,
    SyncRecoveryOutcome,
    SyncTrustState,
)
from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
from mindroom.matrix.sync_loop import (
    OwnRoomMembership,
    own_membership_from_sliding_sync,
    own_membership_from_sync,
    run_matrix_sync_forever,
)
from mindroom.matrix.users import AgentMatrixUser, login_agent_user
from mindroom.matrix_rtc.call_manager import CallManager, maybe_build_call_manager
from mindroom.memory import store_conversation_memory
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.response_delivery import TurnHandoff
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    RuntimeShutdownIntent,
    restart_reason_category_for,
)
from mindroom.stop import StopManager
from mindroom.teams import TeamMode, TeamOutcome, resolve_configured_team
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.tool_approval import is_process_active_approval_card
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.tool_system.worker_routing import tool_execution_identity

from . import constants, interactive
from .agents import create_agent, show_tool_calls_for_agent
from .authorization import is_authorized_sender
from .background_tasks import create_background_task, wait_for_background_tasks
from .coalescing import CoalescingGate
from .coalescing_batch import CoalescingKey, PendingEvent, is_active_follow_up_coalescing_key
from .command_turn_executor import CommandTurnExecutor, CommandTurnExecutorDeps
from .commands import config_confirmation
from .constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_avatar_path
from .conversation_resolver import ConversationResolver, ConversationResolverDeps
from .conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from .delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    ResponseHookService,
    SendTextRequest,
)
from .dispatch_callback_outcome import TurnDispatchOutcome
from .edit_regenerator import EditRegenerator, EditRegeneratorDeps
from .entity_rooms import get_rooms_for_entity
from .event_journal import (
    ApprovalView,
    EventClass,
    EventJournalStore,
    EventKind,
    MembershipFence,
    PrincipalStore,
    SemanticConsumer,
)
from .event_journal_open import OpenEventJournal, bind_event_journal, open_event_journal
from .inbound_turn_normalizer import InboundTurnNormalizer, InboundTurnNormalizerDeps
from .ingress_validation import IngressValidator, IngressValidatorDeps
from .journal_dispatch import (
    JournalCallbacks,
    JournalDispatcher,
)
from .knowledge import KnowledgeAccessSupport
from .logging_config import get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client_room_admin import get_joined_rooms
from .matrix.client_session import MatrixSyncStorage, PermanentMatrixStartupError
from .matrix.conversation_hydration import ConversationHydrator
from .matrix.conversation_reads import ConversationReader, latest_agent_message_snapshot
from .matrix.journal_ingress import event_is_live as journal_event_is_live
from .matrix.relation_lookup import RelationLookup
from .matrix.room_member_joins import (
    RoomMemberJoin,
    emit_room_member_join_at_least_once,
    record_room_member_joins_seen_from_events,
    room_member_sync_state_plan,
    room_member_sync_timeline_events,
)
from .matrix.to_device import AuthenticatedToDeviceEvent
from .media_inputs import MediaInputs
from .reaction_dispatch import ReactionDispatcher, ReactionDispatcherDeps
from .response_payload_preparation import ResponsePayloadPreparer
from .response_runner import ResponseRequest, ResponseRunner, ResponseRunnerDeps, prepare_memory_and_model_context
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    has_deferred_overdue_tasks,
    restore_scheduled_tasks,
)
from .startup_errors import PermanentStartupError
from .sync_restart_retry import InterruptedTurnRooms
from .turn_controller import TurnController, TurnControllerDeps
from .turn_policy import IngressHookRunner, TurnPolicy, TurnPolicyDeps
from .turn_store import TurnStore, TurnStoreDeps
from .user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from .visible_response_reconciliation import VisibleResponseReconciler, VisibleResponseReconcilerDeps
from .visible_voice_echo import VisibleVoiceEchoDeps, VisibleVoiceEchoLifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from datetime import datetime
    from pathlib import Path

    import structlog
    from agno.agent import Agent

    from mindroom.coalescing_batch import CoalescedBatch
    from mindroom.config.main import Config
    from mindroom.matrix.agent_message_snapshot import AgentMessageSnapshot
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.runtime_protocols import OrchestratorRuntime

type _MatrixEventId = str

logger = get_logger(__name__)

__all__ = ["AgentBot", "TeamBot", "create_bot_for_entity"]


# Constants
_SYNC_TIMEOUT_MS = 30000
_CLASSIC_SYNC_REBUILD_BACKOFF_INITIAL_SECONDS = 1.0
_CLASSIC_SYNC_REBUILD_BACKOFF_MAX_SECONDS = 30.0
_DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS = 1.0
_DELIVERY_RECOVERY_RETRY_MAX_DELAY_SECONDS = 30.0
# Raise the per-room timeline limit above the homeserver default (~10) so a
# room has to flood much harder before the server truncates its timeline and
# forces a limited-sync gap backfill. This only widens the timeline window; it
# leaves every other section at server defaults so no event type is filtered
# out.
_SYNC_TIMELINE_LIMIT = 50
_SYNC_FILTER: dict[str, object] = {"room": {"timeline": {"limit": _SYNC_TIMELINE_LIMIT}}}


def _classic_sync_rebuild_backoff_seconds(attempt: int) -> float:
    """Return zero for the first reentry, then capped exponential backoff."""
    if attempt <= 1:
        return 0.0
    delay = _CLASSIC_SYNC_REBUILD_BACKOFF_INITIAL_SECONDS
    for _ in range(attempt - 2):
        delay *= 2
        if delay >= _CLASSIC_SYNC_REBUILD_BACKOFF_MAX_SECONDS:
            return _CLASSIC_SYNC_REBUILD_BACKOFF_MAX_SECONDS
    return delay


@dataclass(frozen=True, slots=True)
class _RoomMemberJoinSyncHookPlan:
    """Room-member join hook actions derived from one sync response."""

    arm_after_response: bool = True
    emit_state: bool = False
    emit_snapshot_state: bool = False
    emit_timeline: bool = False
    record_state_seen: bool = False
    record_timeline_seen: bool = False


def _create_best_effort_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    *,
    owner: BotRuntimeState | None = None,
    admit: Callable[..., bool] | None = None,
) -> Callable[..., Awaitable[None]]:
    """Run one explicitly best-effort callback as a background task.

    Use this only for auxiliary consumers or Matrix inputs without a stable
    source event ID.
    Correctness-critical source-backed events are admitted to the event journal.
    """

    async def wrapper(*args: object, **kwargs: object) -> None:
        if admit is not None and not admit(*args, **kwargs):
            return

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
    config: Config,
    runtime_paths: RuntimePaths,
    storage_path: Path,
    config_path: Path | None = None,
    journal_store: EventJournalStore | None = None,
) -> AgentBot | TeamBot | None:
    """Create appropriate bot instance for an entity (agent, team, or router).

    Args:
        entity_name: Name of the entity to create a bot for
        agent_user: Matrix user for the bot
        config: Configuration object
        runtime_paths: Explicit runtime context for paths, env, and Matrix identity resolution
        storage_path: Path for storing agent data
        config_path: Path to the YAML config file used by config-aware tools
        journal_store: Shared event-journal store to borrow, or None to open one

    Returns:
        Bot instance or None if entity not found in config

    """
    enable_streaming = config.defaults.enable_streaming
    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases), runtime_paths)
        return AgentBot(
            agent_user,
            storage_path,
            config,
            runtime_paths,
            rooms,
            config_path=config_path,
            enable_streaming=enable_streaming,
            journal_store=journal_store,
        )

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms, runtime_paths)
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths,
            rooms=rooms,
            config_path=config_path,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=enable_streaming,
            journal_store=journal_store,
        )

    if entity_name in config.agents:
        agent_config = config.agents[entity_name]
        rooms = resolve_room_aliases(agent_config.rooms, runtime_paths)
        return AgentBot(
            agent_user,
            storage_path,
            config,
            runtime_paths,
            rooms,
            config_path=config_path,
            enable_streaming=enable_streaming,
            journal_store=journal_store,
        )

    msg = f"Entity '{entity_name}' not found in configuration."
    raise ValueError(msg)


class AgentBot:
    """Matrix lifecycle shell for one configured agent or router entity."""

    # Construction inputs
    agent_user: AgentMatrixUser
    storage_path: Path
    runtime_paths: RuntimePaths
    rooms: list[str]
    config_path: Path | None
    logger: structlog.stdlib.BoundLogger
    stop_manager: StopManager

    # Mutable lifecycle state
    running: bool
    last_sync_time: datetime | None
    _last_sync_monotonic: float | None
    _first_sync_done: bool
    _classic_sync_rebuild_pending: bool
    _classic_sync_rebuild_attempt: int
    _sync_shutting_down: bool
    _delivery_recovery_wake: asyncio.Event
    _delivery_recovery_task: asyncio.Task[None] | None

    # Shared runtime state and extracted collaborators
    _hook_registry_state: HookRegistryState
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
    _deferred_overdue_task_drain_task: asyncio.Task[None] | None
    _call_manager: CallManager | None
    _calls_reconcile_pending: bool
    _room_member_callback_registered: bool
    _room_member_join_hooks_armed: bool
    _sliding_sync_startup_warning_emitted: bool
    _turn_controller: TurnController
    _room_lifecycle: BotRoomLifecycle
    _local_departures_awaiting_sync: set[str]
    _sync_continuity_store: SyncContinuityStore
    _sync_checkpoint_trust: SyncCheckpointTrust

    def __init__(
        self,
        agent_user: AgentMatrixUser,
        storage_path: Path,
        config: Config,
        runtime_paths: RuntimePaths,
        rooms: list[str] | None = None,
        config_path: Path | None = None,
        enable_streaming: bool = True,
        journal_store: EventJournalStore | None = None,
    ) -> None:
        """Initialize the bot with canonical runtime-backed config state.

        ``journal_store`` is borrowed when given. One database holds every
        principal in a deployment, so a store per bot means a connection pool
        per bot -- on PostgreSQL that multiplies connections by the number of
        configured entities until the server refuses them, and on SQLite it
        moves write contention from an in-process queue onto the file lock. A
        borrowed store is not closed here, because its owner outlives this bot.
        """
        self._borrowed_journal_store = journal_store
        # Set when this bot opens its own, which only happens when nothing was
        # handed to it. What this bot opened is what this bot closes.
        self._own_journal: OpenEventJournal | None = None
        self.agent_user = agent_user
        self.storage_path = storage_path
        self.runtime_paths = runtime_paths
        self.rooms = [] if rooms is None else rooms
        self.config_path = config_path
        self.logger = logger.bind(agent=self.agent_name)
        self.stop_manager = StopManager()
        self._interrupted_turn_rooms = InterruptedTurnRooms()
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._classic_sync_rebuild_pending = False
        self._classic_sync_rebuild_attempt = 0
        self._orchestrator_ready_handled = False
        # The Matrix device this bot sends as, captured at login rather than
        # read off the client per send. A transaction ID only deduplicates
        # within the device that used it, so the outbox records this next to
        # every claim; before login there is no answer, and `None` says so.
        self._sending_device_id: str | None = None
        self._sync_shutting_down = False
        self._delivery_recovery_wake = asyncio.Event()
        self._delivery_recovery_task = None
        self._hook_registry_state = HookRegistryState(HookRegistry.empty())
        self._room_member_callback_registered = False
        self._room_member_join_hooks_armed = False
        self._room_member_join_lock = asyncio.Lock()
        self._sliding_sync_startup_warning_emitted = False
        self._runtime_view = BotRuntimeState(
            client=None,
            config=config,
            runtime_paths=self.runtime_paths,
            enable_streaming=enable_streaming,
            orchestrator=None,
        )
        self._sync_continuity_store = SyncContinuityStore(self.storage_path, self.agent_name)
        self._sync_checkpoint_trust = SyncCheckpointTrust(
            continuity_store=self._sync_continuity_store,
            logger=self.logger,
            # Resolved on first use rather than now: the journal store is built
            # further down, and trusting a saved token means asserting that
            # store already holds every event the token covers.
            store_generation_provider=self._resolve_journal_generation,
            history_recovery_provider=self._journal_principal,
        )
        self._deferred_overdue_task_drain_task = None
        self._call_manager: CallManager | None = None
        self._calls_reconcile_pending = False
        self._local_departures_awaiting_sync = set()

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

        self._room_lifecycle = BotRoomLifecycle(
            BotRoomLifecycleDeps(
                agent_name=self.agent_name,
                agent_user=self.agent_user,
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                continuity_store=self._sync_continuity_store,
                get_logger=lambda: self.logger,
                get_configured_rooms=lambda: self.rooms,
                send_response=send_room_lifecycle_response,
                on_room_joined=self._on_room_joined,
                on_configured_room_joined=self._post_join_room_setup,
                on_room_left=self._fence_left_room,
            ),
        )
        self._init_runtime_components()

    async def _resolve_journal_generation(self) -> str | None:
        """Return the event journal's durable identity, refusing a database that is not ours.

        A bot built outside the orchestrator opens its own store, so this is
        the first async moment at which that store can be checked against the
        journal this install is bound to. Orchestrator-run bots borrow a store
        the startup bind already accepted, and reach the same answer here.
        """
        return await bind_event_journal(
            self._journal_store,
            journal_config=self.config.event_journal,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
        )

    def _journal_principal(self) -> PrincipalStore:
        """Return this bot's principal-bound store, once the journal is open."""
        return self._journal_store.principal(self._journal_principal_id)

    def _open_own_journal(self) -> OpenEventJournal:
        """Open the durable store this bot's journal, projection, and outbox share.

        One database can hold every bot in the process; each receives only its
        own principal-bound view. Only a bot built outside the orchestrator
        reaches this, and it owns what it opened: the returned journal is
        closed in :meth:`stop`, where a borrowed store is not.
        """
        return open_event_journal(
            self.config.event_journal,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
        )

    def _init_runtime_components(self) -> None:
        """Initialize runtime-only helpers that depend on bound instance methods."""
        if not self.agent_user.user_id:
            msg = f"Missing Matrix ID for {self.agent_name!r} during runtime initialization"
            raise PermanentMatrixStartupError(msg)
        runtime_matrix_id = self.matrix_id
        self._journal_principal_id = f"{self.agent_name}@{runtime_matrix_id.full_id}"
        borrowed = self._borrowed_journal_store
        if borrowed is not None:
            self._journal_store = borrowed
        else:
            # Reached a second time when a login authenticates as a different
            # Matrix user. The principal changes with the identity, but the
            # database does not: one database holds every principal, so the new
            # principal's view comes from the store this bot already opened.
            # Opening a second would abandon the first without ever closing it.
            if self._own_journal is None:
                self._own_journal = self._open_own_journal()
            self._journal_store = self._own_journal.store
        self._coalescing_gate = CoalescingGate(
            dispatch_batch=self._dispatch_coalesced_batch,
            debounce_seconds=lambda: self.config.defaults.coalescing.debounce_ms / 1000,
            is_shutting_down=lambda: self._sync_shutting_down,
            wait_until_dispatch_allowed=self._wait_until_coalesced_dispatch_allowed,
            room_scope_is_single_conversation=self._room_scope_is_single_conversation,
            dispatch_allowed_now=self._coalesced_dispatch_allowed_now,
            timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(
                timestamp_ms,
                timezone=self.config.timezone,
            ),
            on_dispatch_failure=self._retry_failed_coalesced_dispatch,
            on_undelivered_source=self._retry_pending_dispatch_source,
            on_intentionally_ignored_source=self._settle_ignored_dispatch_source,
        )
        self._hook_context_support = HookContextSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            agent_name=self.agent_name,
            hook_registry_state=self._hook_registry_state,
            hook_send_message=self._hook_send_message,
            agent_message_snapshot_reader=self._hook_agent_message_snapshot,
        )
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
        self._membership_fence = MembershipFence(
            store=self._journal_store.principal(self._journal_principal_id),
        )
        self._relations = RelationLookup(
            store=self._journal_store.principal(self._journal_principal_id),
            runtime=self._runtime_view,
        )
        self._conversation_reader = ConversationReader(
            store=self._journal_store.principal(self._journal_principal_id),
            hydrator=ConversationHydrator(
                store=self._journal_store.principal(self._journal_principal_id),
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
        self._delivery_gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                logger=self.logger,
                resolver=self._conversation_resolver,
                redact_message_event=self._redact_message_event,
                response_hooks=ResponseHookService(
                    hook_context=self._hook_context_support,
                ),
                outbox=self._journal_store.principal(self._journal_principal_id),
                turn_handoff=TurnHandoff(
                    sources_for_turn=self._delivered_turn_source_ids,
                    # Resolved late: the dispatcher is built after the gateway.
                    released=lambda event_ids: self._journal_dispatcher.release_delivered_turn_sources(event_ids),
                ),
                # Resolved late for the same reason: this gateway is built
                # before the bot has logged in, and a re-login replaces the
                # device that the frozen transaction IDs belong to.
                sending_device_id=lambda: self._sending_device_id,
                # Deferred for the same reason as the device: the turn store is
                # built after this gateway. Consulted only once a FINAL send has
                # produced an event ID, so the acknowledgement can carry the
                # record that needs to know it.
                terminal_turn_for=lambda turn_id, event_id: self._turn_store.terminal_turn_record(turn_id, event_id),
                terminal_turn_committed=lambda turn_id, event_id: self._turn_store.publish_committed_response(
                    turn_id,
                    event_id,
                ),
            ),
        )
        self._tool_runtime_support = ToolRuntimeSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
            agent_name=self.agent_name,
            matrix_id=runtime_matrix_id,
            resolver=self._conversation_resolver,
            hook_context=self._hook_context_support,
        )
        self._turn_store = TurnStore(
            TurnStoreDeps(
                agent_name=self.agent_name,
                turn_records=self._journal_store.turn_records(self.agent_name),
                legacy_responses_file=legacy_responses_file_path(self.storage_path, self.agent_name),
                state_writer=self._conversation_state_writer,
                resolver=self._conversation_resolver,
                tool_runtime=self._tool_runtime_support,
            ),
        )
        self._journal_dispatcher = JournalDispatcher(
            store=self._journal_store.principal(self._journal_principal_id),
            self_sender=runtime_matrix_id.full_id,
            callbacks=JournalCallbacks(
                on_message=self._on_message,
                on_media=self._on_media_message,
                on_reaction=self._on_reaction,
                on_approval=self._on_unknown_event,
                on_room_lifecycle=self._on_room_member,
                on_redaction=self._on_redaction,
                on_decryption_failure=self._on_decryption_failure,
                source_has_live_owner=self._coalescing_gate.has_pending_source_event,
                turn_has_live_claim=self._turn_store.has_live_turn_claim,
            ),
            room_for_id=self._room_for_journal_event,
            on_persist_failure=self._record_dispatch_persist_failure,
            room_lifecycle_admission_enabled=lambda: (
                self.agent_name == ROUTER_AGENT_NAME and self._first_sync_done and self._room_member_join_hooks_armed
            ),
        )
        self._post_response_effects_support = PostResponseEffectsSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            delivery_gateway=self._delivery_gateway,
            conversation_reader=self._conversation_reader,
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
        self._response_runner = ResponseRunner(
            ResponseRunnerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                stop_manager=self.stop_manager,
                runtime_paths=self.runtime_paths,
                storage_path=self.storage_path,
                agent_name=self.agent_name,
                matrix_full_id=runtime_matrix_id.full_id,
                resolver=self._conversation_resolver,
                tool_runtime=self._tool_runtime_support,
                knowledge_access=self._knowledge_access_support,
                delivery_gateway=self._delivery_gateway,
                post_response_effects=self._post_response_effects_support,
                state_writer=self._conversation_state_writer,
                request_preparer=self._request_payload_preparer,
            ),
        )
        self._edit_regenerator = EditRegenerator(
            EditRegeneratorDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                resolver=self._conversation_resolver,
                turn_store=self._turn_store,
                ingress_hook_runner=self._ingress_hook_runner,
                generate_response=lambda request: self._run_regenerated_response(request),
                wait_for_turn_settled=self._turn_store.wait_for_turn_settled,
                receipt_order=self._journal_dispatcher.receipt_order,
                interrupted_turn_rooms=self._interrupted_turn_rooms,
                timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(
                    timestamp_ms,
                    timezone=self.config.timezone,
                ),
            ),
        )
        self._turn_policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
            ),
        )
        self._ingress_validator = IngressValidator(
            IngressValidatorDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                matrix_id=runtime_matrix_id,
                turn_store=self._turn_store,
                turn_policy=self._turn_policy,
            ),
        )
        self._visible_voice_echo = VisibleVoiceEchoLifecycle(
            VisibleVoiceEchoDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                agent_name=self.agent_name,
                delivery_gateway=self._delivery_gateway,
                turn_store=self._turn_store,
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
            ),
        )
        self._user_stop_reconciler = UserStopReconciler(
            UserStopReconcilerDeps(
                turn_store=self._turn_store,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
            ),
        )
        self._turn_controller = TurnController(
            TurnControllerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                relations=self._relations,
                pending_turns=self._journal_store.principal(self._journal_principal_id),
                resolver=self._conversation_resolver,
                normalizer=self._inbound_turn_normalizer,
                command_executor=self._command_turn_executor,
                turn_policy=self._turn_policy,
                ingress_hook_runner=self._ingress_hook_runner,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                tool_runtime=self._tool_runtime_support,
                turn_store=self._turn_store,
                coalescing_gate=self._coalescing_gate,
                edit_regenerator=self._edit_regenerator,
                ingress=self._ingress_validator,
                interrupted_turn_rooms=self._interrupted_turn_rooms,
                visible_voice_echo=self._visible_voice_echo,
                visible_responses=self._visible_responses,
                retry_dispatch_sources=self._journal_dispatcher.retry_turn_sources,
            ),
        )
        self._reaction_dispatcher = ReactionDispatcher(
            ReactionDispatcherDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                journal_dispatcher=self._journal_dispatcher,
                turn_policy=self._turn_policy,
                turn_store=self._turn_store,
                stop_manager=self.stop_manager,
                user_stop_reconciler=self._user_stop_reconciler,
                ingress=self._ingress_validator,
                reserve_prompt_ingress_order=self._turn_controller.reserve_prompt_ingress_order,
                handle_interactive_selection=self._turn_controller.handle_interactive_selection,
                emit_reaction_received_hooks=self._emit_reaction_received_hooks,
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

    async def _wait_until_coalesced_dispatch_allowed(self, key: CoalescingKey) -> None:
        """Hold active follow-up dispatch until the response lock for its target is idle."""
        if not is_active_follow_up_coalescing_key(key):
            return
        await self._response_runner.wait_for_thread_response_idle(key.room_id, key.thread_id)

    def _coalesced_dispatch_allowed_now(self, key: CoalescingKey) -> bool:
        """Return whether one coalescing key's target has no active response right now."""
        return key.thread_id not in self._response_runner.active_thread_ids_for_room(key.room_id)

    def _room_scope_is_single_conversation(self, room_id: str) -> bool:
        """Return whether this agent treats the whole room as one conversation."""
        return (
            self.config.get_entity_thread_mode(
                self.agent_name,
                self.runtime_paths,
                room_id=room_id,
            )
            == "room"
        )

    def _rebuild_runtime_components_after_login_if_identity_changed(self, matrix_id_before_login: MatrixID) -> None:
        """Refresh startup collaborators when Matrix login authenticates as a different user."""
        if self.agent_user.user_id == matrix_id_before_login.full_id:
            return

        self.agent_user.__dict__.pop("matrix_id", None)
        self.__dict__.pop("matrix_id", None)
        self._init_runtime_components()

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
        """Return the canonical live config."""
        return self._runtime_view.config

    @config.setter
    def config(self, value: Config) -> None:
        """Update the canonical live config."""
        self._runtime_view.config = value

    @property
    def enable_streaming(self) -> bool:
        """Return whether streaming is enabled for this bot."""
        return self._runtime_view.enable_streaming

    @enable_streaming.setter
    def enable_streaming(self, value: bool) -> None:
        """Update whether streaming is enabled for this bot."""
        self._runtime_view.enable_streaming = value

    @property
    def orchestrator(self) -> OrchestratorRuntime | None:
        """Return the current orchestrator."""
        return self._runtime_view.orchestrator

    @orchestrator.setter
    def orchestrator(self, value: OrchestratorRuntime | None) -> None:
        """Update the current orchestrator."""
        self._runtime_view.orchestrator = value

    @property
    def approval_cards(self) -> ApprovalView:
        """Return this bot's durable store of approval cards awaiting a decision."""
        return self._journal_store.principal(self._journal_principal_id)

    @property
    def runtime_started_at(self) -> float:
        """Return when this bot runtime started."""
        return self._runtime_view.runtime_started_at

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None:
        """Return the latest event id for one Matrix thread when the projection knows it."""
        return await self._conversation_reader.latest_thread_event_id(room_id=room_id, thread_id=thread_id)

    @property
    def hook_registry(self) -> HookRegistry:
        """Return the currently active hook registry."""
        return self._hook_registry_state.registry

    @hook_registry.setter
    def hook_registry(self, value: HookRegistry) -> None:
        """Update the active hook registry."""
        self._hook_registry_state.registry = value

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of active response lifecycles."""
        return self._response_runner.in_flight_response_count

    @property
    def admission_gate(self) -> ResponseAdmissionGate:
        """Return the gate deciding whether responses may start right now."""
        return self._runtime_view.response_admission_gate

    @admission_gate.setter
    def admission_gate(self, value: ResponseAdmissionGate) -> None:
        """Bind the orchestrator-owned response-admission gate."""
        self._runtime_view.response_admission_gate = value

    @property
    def pending_sync_restart_retry_room_ids(self) -> frozenset[str]:
        """Return rooms with interrupted turns awaiting replacement recovery."""
        return self._interrupted_turn_rooms.pending_room_ids

    @property
    def approval_room_ids(self) -> frozenset[str]:
        """Return configured and durably invited rooms owned by approval transport."""
        return frozenset(
            room_id for room_id in (*self.rooms, *self._room_lifecycle.invited_rooms) if room_id.startswith("!")
        )

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

    async def _emit_reaction_received_hooks(
        self,
        *,
        room_id: str,
        event: nio.ReactionEvent,
        correlation_id: str,
    ) -> None:
        """Emit reaction:received after built-in handlers decline the reaction."""
        assert self.client is not None
        if not self.hook_registry.has_hooks(EVENT_REACTION_RECEIVED):
            return

        normalized_target_event_id = event.reacts_to.strip()
        thread_id: str | None = None
        if normalized_target_event_id:
            try:
                thread_id = (
                    await self._conversation_resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort(
                        room_id,
                        normalized_target_event_id,
                    )
                )
            except Exception as exc:
                self.logger.debug(
                    "Failed to resolve reaction target thread for hook context",
                    room_id=room_id,
                    target_event_id=normalized_target_event_id,
                    error=str(exc),
                )

        context = ReactionReceivedContext(
            **self._hook_context_support.base_kwargs(EVENT_REACTION_RECEIVED, correlation_id),
            room_id=room_id,
            event_id=event.event_id,
            sender_id=event.sender,
            reaction_key=event.key,
            target_event_id=event.reacts_to,
            thread_id=thread_id,
        )
        await emit(self.hook_registry, EVENT_REACTION_RECEIVED, context)

    async def _emit_room_member_joined_hooks(self, join: RoomMemberJoin) -> None:
        """Emit room:member_joined for one live human Matrix room join."""
        if not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            return

        context = RoomMemberJoinedContext(
            **self._hook_context_support.base_kwargs(EVENT_ROOM_MEMBER_JOINED, join.event_id),
            agent_name=self.agent_name,
            room_id=join.room_id,
            event_id=join.event_id,
            user_id=join.user_id,
            sender_id=join.sender_id,
            display_name=join.display_name,
            avatar_url=join.avatar_url,
            membership=join.membership,
            prev_membership=join.prev_membership,
        )
        await emit(self.hook_registry, EVENT_ROOM_MEMBER_JOINED, context)

    async def _emit_agent_lifecycle_event(
        self,
        event_name: str,
        *,
        stop_reason: str | None = None,
    ) -> None:
        """Emit one agent lifecycle observer event for this bot."""
        if not self.hook_registry.has_hooks(event_name):
            return

        matrix_user_id = self.agent_user.user_id or self.matrix_id.full_id
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
            matrix_user_id=matrix_user_id,
            joined_room_ids=joined_room_ids,
            stop_reason=stop_reason,
        )
        await emit(self.hook_registry, event_name, context)

    @property
    def show_tool_calls(self) -> bool:
        """Whether to show tool call details inline in responses."""
        return show_tool_calls_for_agent(self.config, self.agent_name)

    @property  # Not cached_property because Team mutates it!
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        if self.agent_name != ROUTER_AGENT_NAME and self.config.agents[self.agent_name].private is not None:
            msg = (
                f"AgentBot.agent is only available for shared agents. "
                f"Private agent '{self.agent_name}' requires an explicit execution identity."
            )
            raise ValueError(msg)
        assert self.orchestrator is not None
        knowledge = self._knowledge_access_support.for_agent(self.agent_name)
        return create_agent(
            agent_name=self.agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            knowledge=knowledge,
            execution_identity=None,
            hook_registry=self.hook_registry,
            refresh_scheduler=self.orchestrator.knowledge_refresh_scheduler,
        )

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        await self._room_lifecycle.join_configured_rooms()

    async def _post_join_room_setup(self, room_id: str) -> None:
        """Run room setup that should happen after joins and across restarts."""
        if self._call_manager is not None:
            self._calls_reconcile_pending = True
        if self.agent_name != ROUTER_AGENT_NAME:
            return

        assert self.client is not None

        restored_tasks = await restore_scheduled_tasks(
            self.client,
            room_id,
            self.config,
            self.runtime_paths,
            self._conversation_reader,
        )
        if restored_tasks > 0:
            self.logger.info("restored_scheduled_tasks", room_id=room_id, restored_task_count=restored_tasks)

        restored_configs = await config_confirmation.restore_pending_changes(self.client, room_id)
        if restored_configs > 0:
            self.logger.info(
                "restored_pending_config_changes",
                room_id=room_id,
                restored_config_count=restored_configs,
            )

        await self._send_welcome_message_if_empty(room_id)

        if self._first_sync_done:
            self._maybe_start_deferred_overdue_task_drain()

    async def _on_room_joined(self, room_id: str) -> None:
        """Stop treating a room as departed once the homeserver confirms the join."""
        self._local_departures_awaiting_sync.discard(room_id)
        await self._membership_fence.note_membership_restarted(room_id)

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for."""
        await self._room_lifecycle.leave_unconfigured_rooms()

    async def ensure_user_account(self) -> None:
        """Verify that orchestrator account preparation supplied this bot's account."""
        if self.agent_user.user_id:
            return
        msg = f"Matrix account for {self.agent_name!r} was not prepared before bot startup"
        raise PermanentMatrixStartupError(msg)

    async def _set_avatar_if_available(self) -> None:
        """Set avatar for the agent if an avatar file exists."""
        if not self.client:
            return

        entity_type = "teams" if self.agent_name in self.config.teams else "agents"
        avatar_path = resolve_avatar_path(entity_type, self.agent_name, runtime_paths=self.runtime_paths)

        if avatar_path.exists():
            try:
                success = await check_and_set_avatar(self.client, avatar_path)
                if success:
                    self.logger.info("avatar_set")
                else:
                    self.logger.warning("avatar_set_failed")
            except Exception as e:
                self.logger.warning("avatar_set_failed", error=str(e))

    async def _set_presence_with_model_info(self) -> None:
        """Set presence status with model information."""
        if self.client is None:
            return

        status_msg = build_agent_status_message(
            self.agent_name,
            self.config,
            voice_calls_available=(self._call_manager is not None and self._call_manager.voice_backend_available),
        )
        await set_presence_status(self.client, status_msg)

    def mark_sync_loop_started(self) -> None:
        """Record that a sync loop iteration is starting.

        Does NOT arm the monotonic watchdog clock — that only starts when the
        first ``SyncResponse`` or ``SyncError`` arrives.  The watchdog has its
        own startup timeout for the pre-first-response window.
        """
        self._sync_shutting_down = False
        self._response_runner.resume_pending_admissions()
        self._calls_reconcile_pending = self._call_manager is not None
        mark_matrix_sync_loop_started(self.agent_name)

    def reset_watchdog_clock(self) -> None:
        """Reset the monotonic watchdog clock for a fresh sync iteration."""
        self._last_sync_monotonic = None

    async def _prepare_matrix_sync_continuity(self) -> None:
        """Apply the certified startup position to the authenticated Matrix client."""
        client = self.client
        assert client is not None
        classic = self.config.matrix_sync.mode == "classic"
        sync_token = await self._sync_checkpoint_trust.prepare_startup()
        if classic:
            client.clear_persisted_sync_recovery()
            self._classic_sync_rebuild_pending = True
            self._classic_sync_rebuild_attempt = 0
        client.next_batch = sync_token or ""

    async def _apply_sync_response_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        recovery: SyncRecoveryOutcome,
        joined_room_ids: Iterable[str] = (),
    ) -> SyncCertificationDecision:
        """Advance sync continuity after prerequisite durable work completes."""
        client = self.client
        applied = await self._sync_checkpoint_trust.apply_response(
            decision,
            recovery=recovery,
            joined_room_ids=joined_room_ids,
            publish_record=partial(
                self._publish_classic_sync_commit,
                client,
                acknowledge=not decision.reset_client_token,
            ),
        )
        await self._apply_client_rewind_decision(applied)
        return applied

    def _publish_classic_sync_commit(
        self,
        client: nio.AsyncClient | None,
        record: SyncContinuityRecord,
        *,
        acknowledge: bool,
    ) -> None:
        """Publish one durable continuity record and acknowledge its exact nio state.

        A checkpoint that skips an unrecoverable gap is certified together with
        a client reset, because nio may still hold recovery state for the room
        the checkpoint moved past and would refuse to acknowledge it. The reset
        discards that state, so acknowledgement is skipped rather than raised.
        """
        self._room_lifecycle.apply_continuity_record(record)
        if (
            acknowledge
            and client is not None
            and record.checkpoint is not None
            and client.has_uncommitted_classic_sync_state
        ):
            client.acknowledge_classic_sync(record.checkpoint.token)

    async def _apply_client_rewind_decision(
        self,
        decision: SyncCertificationDecision,
    ) -> None:
        """Discard rejected Classic staging and restore durable continuity."""
        if not decision.reset_client_token:
            return
        await self._reset_classic_sync_state(force=True)

    async def _reset_classic_sync_state(self, *, force: bool = False) -> tuple[bool, bool]:
        """Replace nio's transient Classic world with the committed checkpoint."""
        client = self.client
        retry_token = self._sync_checkpoint_trust.retry_token()
        if client is None:
            return False, retry_token is not None
        staged = client.has_uncommitted_classic_sync_state
        if not force and not staged and (client.next_batch or None) == retry_token:
            return False, retry_token is not None
        reset_completed = False
        try:
            await client.reset_classic_sync_state()
            reset_completed = True
        finally:
            if reset_completed or not client.has_uncommitted_classic_sync_state:
                client.next_batch = retry_token or ""
                self._classic_sync_rebuild_pending = True
                self._classic_sync_rebuild_attempt += 1
                self._room_member_join_hooks_armed = False
        return True, retry_token is not None

    async def _reconcile_classic_sync_cursor_after_loop_exit(self) -> None:
        """Settle aborted-response state and restore certified continuity."""
        if self.config.matrix_sync.mode != "classic":
            return
        admission_failed = self._sync_checkpoint_trust.reject_response_before_certification()
        client = self.client
        rewound, has_retry_token = await self._reset_classic_sync_state(
            force=admission_failed or (client is not None and client.has_uncommitted_classic_sync_state),
        )
        if not rewound:
            return
        self.logger.warning(
            "matrix_sync_receive_loop_exit_rewound_uncertified_cursor",
            has_retry_token=has_retry_token,
        )

    async def _rewind_sync_after_pre_certification_failure(self) -> None:
        """Replay a classic sync that failed before its position was certified."""
        rewound, has_retry_token = await self._reset_classic_sync_state(force=True)
        if not rewound:
            return
        self.logger.warning(
            "pre_certification_sync_side_effect_failed_replaying_sync",
            has_retry_token=has_retry_token,
        )

    def _apply_transport_recovery_outcome(
        self,
        *,
        unrecovered_room_ids: frozenset[str],
        transport: str,
    ) -> None:
        """Expose incomplete transport recovery through operator telemetry."""
        if not unrecovered_room_ids:
            return
        self.logger.warning(
            "matrix_sync_recovery_incomplete",
            transport=transport,
            unrecovered_room_ids=sorted(unrecovered_room_ids),
        )

    def _record_dispatch_persist_failure(self) -> None:
        """Let NIO retry rejected source work before replaying safe continuity."""
        self._sync_checkpoint_trust.record_dispatch_persist_failure()

    async def _handle_pre_certification_failure(self) -> None:
        """Reject a response whose prerequisite durable work raised."""
        self._sync_checkpoint_trust.reject_response_before_certification()
        await self._reset_classic_sync_state(force=True)

    async def _apply_sync_response_after_dispatch_acceptance(
        self,
        decision: SyncCertificationDecision,
        *,
        recovery: SyncRecoveryOutcome,
        room_member_join_hook_plan: _RoomMemberJoinSyncHookPlan,
        response: nio.SyncResponse,
    ) -> tuple[SyncCertificationDecision, _RoomMemberJoinSyncHookPlan, bool]:
        """Apply certification only when every source callback reached durable ownership."""
        if self._sync_checkpoint_trust.consume_dispatch_persist_failure():
            self._sync_checkpoint_trust.reject_response_before_certification()
            await self._rewind_sync_after_pre_certification_failure()
            return decision, _RoomMemberJoinSyncHookPlan(arm_after_response=False), True
        applied = await self._apply_sync_response_decision(
            decision,
            recovery=recovery,
            joined_room_ids=response.rooms.join,
        )
        if applied.reset_client_token:
            room_member_join_hook_plan = _RoomMemberJoinSyncHookPlan(arm_after_response=False)
        return applied, room_member_join_hook_plan, applied.reset_client_token

    def seconds_since_last_sync_activity(self) -> float | None:
        """Return elapsed seconds since the last sync-loop activity seen by the watchdog."""
        if self._last_sync_monotonic is None:
            return None
        return time.monotonic() - self._last_sync_monotonic

    def sync_cache_write_progress(self) -> SyncCacheWriteProgress | None:
        """Return the durable sync-cache phase shared by watchdog and health."""
        return get_matrix_sync_cache_write_progress(self.agent_name)

    def _mark_sync_progress(self) -> None:
        """Advance watchdog and health freshness from one sync progress event."""
        self.last_sync_time = mark_matrix_sync_success(self.agent_name)
        self._last_sync_monotonic = time.monotonic()

    def _register_room_member_callback_after_initial_sync(self) -> None:
        """Start listening for live member joins after startup history is drained."""
        if self.agent_name != ROUTER_AGENT_NAME or self._room_member_callback_registered:
            return
        client = self.client
        if client is None:
            return
        client.add_event_callback(self._create_room_member_task_wrapper(), nio.RoomMemberEvent)
        self._room_member_callback_registered = True

    def _create_room_member_task_wrapper(self) -> Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]]:
        """Wake the journal worker for a membership event it already admitted."""

        async def wrapper(room: nio.MatrixRoom, event: nio.Event) -> None:
            del room, event
            self._journal_dispatcher.wake()

        return wrapper

    def _room_member_join_sync_hook_plan(
        self,
        *,
        first_sync_response: bool,
        checkpoint_rebuild_response: bool,
        live_checkpoint_rebuild_response: bool,
        hooks_were_armed: bool,
        decision: SyncCertificationDecision,
    ) -> _RoomMemberJoinSyncHookPlan:
        """Return room-member join hook actions for one certified sync response."""
        if decision.reset_client_token:
            return _RoomMemberJoinSyncHookPlan(arm_after_response=False)
        # The first restored-token sync is requested with full_state=True, so its
        # state block is a current snapshot. Only the timeline is a catch-up stream.
        emit_certified_state = (
            decision.state is SyncTrustState.CERTIFIED and not first_sync_response and hooks_were_armed
        )
        tokenless_baseline = self._sync_checkpoint_trust.tokenless_baseline_pending()
        return _RoomMemberJoinSyncHookPlan(
            arm_after_response=True,
            emit_state=emit_certified_state,
            emit_snapshot_state=live_checkpoint_rebuild_response,
            emit_timeline=checkpoint_rebuild_response,
            record_state_seen=(
                tokenless_baseline
                or (
                    decision.state is SyncTrustState.CERTIFIED
                    and not emit_certified_state
                    and not live_checkpoint_rebuild_response
                )
            ),
            record_timeline_seen=tokenless_baseline,
        )

    async def _run_pre_certification_sync_response_side_effects(
        self,
        response: nio.SyncResponse,
        *,
        room_member_join_hook_plan: _RoomMemberJoinSyncHookPlan,
    ) -> None:
        """Finish source-backed lifecycle work before certifying its sync position."""
        if room_member_join_hook_plan.record_state_seen:
            await self._emit_room_member_joined_sync_state_hooks(
                response,
                record_only=True,
                include_timeline_baseline=room_member_join_hook_plan.record_timeline_seen,
            )
        if room_member_join_hook_plan.emit_snapshot_state:
            await self._emit_room_member_joined_sync_state_hooks(
                response,
                dispatch_snapshot_joins=True,
            )
        if room_member_join_hook_plan.emit_timeline:
            await self._emit_room_member_joined_sync_timeline_hooks(response)
        if room_member_join_hook_plan.emit_state:
            await self._emit_room_member_joined_sync_state_hooks(response)

    async def _recover_unacknowledged_deliveries(self) -> bool:
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
        if self._sync_shutting_down:
            return
        self._delivery_recovery_wake.set()
        task = self._delivery_recovery_task
        if task is not None and not task.done():
            return
        self._delivery_recovery_task = create_background_task(
            self._run_scheduled_delivery_recovery(),
            name=f"delivery_recovery_{self.agent_name}",
            owner=self._runtime_view,
        )

    async def _run_scheduled_delivery_recovery(self) -> None:
        """Recover outbox debt without making Matrix receive progress wait."""
        retry_delay = _DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS
        try:
            while not self._sync_shutting_down:
                self._delivery_recovery_wake.clear()
                complete = await self._recover_unacknowledged_deliveries()
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
            if self._delivery_recovery_task is asyncio.current_task():
                self._delivery_recovery_task = None

    async def _run_sync_response_side_effects(
        self,
        *,
        first_sync_response: bool,
    ) -> None:
        """Run side effects that do not own raw sync checkpoint safety."""
        if first_sync_response:
            self._register_room_member_callback_after_initial_sync()
        self._schedule_delivery_recovery()
        if first_sync_response:
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)

        orchestrator = self.orchestrator
        if not self._orchestrator_ready_handled and orchestrator is not None:
            await orchestrator.handle_bot_ready(self)
            self._orchestrator_ready_handled = True

        if first_sync_response or has_deferred_overdue_tasks():
            self._maybe_start_deferred_overdue_task_drain()

    async def _handle_classic_sync_response(
        self,
        response: nio.SyncResponse,
        *,
        first_sync_response: bool,
        room_member_join_hooks_were_armed: bool,
    ) -> tuple[_RoomMemberJoinSyncHookPlan, bool]:
        """Apply one Classic response through transport and cache owners."""
        self._apply_transport_recovery_outcome(
            unrecovered_room_ids=response.unrecovered_room_ids,
            transport="classic",
        )
        with track_matrix_sync_cache_write(self.agent_name):
            try:
                await self._apply_own_room_membership_from_sync(response)
            except BaseException:
                await self._handle_pre_certification_failure()
                raise
            checkpoint_rebuild_response = (
                first_sync_response or self._classic_sync_rebuild_pending
            ) and self._sync_checkpoint_trust.retry_token() is not None
            live_checkpoint_rebuild_response = (
                self._classic_sync_rebuild_pending
                and not first_sync_response
                and self._sync_checkpoint_trust.retry_token() is not None
            )
            # No await between the guarded membership apply above and this plan,
            # so there is no window in which this response could be abandoned
            # after durable work and before its verdict. The cancellation branch
            # that used to certify a fail-closed result here existed for the
            # cache write, which was the only thing awaited in that gap.
            recovery = self._sync_checkpoint_trust.observed_recovery(response)
            decision = self._sync_checkpoint_trust.plan_response(
                next_batch=response.next_batch,
                recovery=recovery,
            )
            room_member_join_hook_plan = self._room_member_join_sync_hook_plan(
                first_sync_response=first_sync_response,
                checkpoint_rebuild_response=checkpoint_rebuild_response,
                live_checkpoint_rebuild_response=live_checkpoint_rebuild_response,
                hooks_were_armed=room_member_join_hooks_were_armed,
                decision=decision,
            )
            try:
                await self._run_pre_certification_sync_response_side_effects(
                    response,
                    room_member_join_hook_plan=room_member_join_hook_plan,
                )
            except BaseException:
                await self._handle_pre_certification_failure()
                raise
            try:
                (
                    _decision,
                    room_member_join_hook_plan,
                    rejected_response,
                ) = await self._apply_sync_response_after_dispatch_acceptance(
                    decision,
                    recovery=recovery,
                    room_member_join_hook_plan=room_member_join_hook_plan,
                    response=response,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.logger.warning(
                    "matrix_sync_certification_apply_failed",
                    error=str(error),
                    error_type=type(error).__name__,
                )
                raise
        self._mark_sync_progress()
        return room_member_join_hook_plan, rejected_response

    async def _handle_sliding_sync_response(self, response: nio.SlidingSyncResponse) -> None:
        """Apply one Sliding response through membership and transport owners."""
        self._sync_checkpoint_trust.acknowledge_dispatch_persist_failures()
        with track_matrix_sync_cache_write(self.agent_name):
            await self._apply_own_room_membership_from_sliding_sync(response)
        self._apply_transport_recovery_outcome(
            unrecovered_room_ids=response.unrecovered_room_ids,
            transport="sliding",
        )
        if response.pos and not response.unrecovered_room_ids and self._room_lifecycle.has_pending_join_decrypt_fences:
            await self._room_lifecycle.observe_trusted_sync_rooms(
                room_id for room_id, room in response.rooms.items() if room.membership == "join"
            )
        self._mark_sync_progress()

    async def _on_sync_response(self, _response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Track successful sync responses for health checks and watchdogs."""
        try:
            await self._apply_sync_response(_response)
        finally:
            # nio states an event's provenance once, to admission, and only for
            # the response carrying it. Anything this response's own consumers
            # did not read is stale the moment the response is done.
            self._journal_dispatcher.timeline_member_provenance.clear()

    async def _apply_sync_response(self, _response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Apply one certified sync response through its transport owners."""
        first_sync_response = not self._first_sync_done
        rejected_response = False
        room_member_join_hooks_were_armed = self._room_member_join_hooks_armed
        room_member_join_hook_plan = _RoomMemberJoinSyncHookPlan()
        self._mark_sync_progress()

        if self._sync_shutting_down:
            return

        if isinstance(_response, nio.SyncResponse):
            (
                room_member_join_hook_plan,
                rejected_response,
            ) = await self._handle_classic_sync_response(
                _response,
                first_sync_response=first_sync_response,
                room_member_join_hooks_were_armed=room_member_join_hooks_were_armed,
            )
        elif isinstance(_response, nio.SlidingSyncResponse):
            await self._handle_sliding_sync_response(_response)
        if rejected_response:
            return
        if isinstance(_response, nio.SyncResponse):
            self._classic_sync_rebuild_pending = False
            self._classic_sync_rebuild_attempt = 0
        self._first_sync_done = True
        self._room_member_join_hooks_armed = room_member_join_hook_plan.arm_after_response

        await self._run_sync_response_side_effects(
            first_sync_response=first_sync_response,
        )
        if self._calls_reconcile_pending:
            self._calls_reconcile_pending = False
            call_manager = self._call_manager
            if call_manager is not None:
                create_background_task(
                    call_manager.reconcile_joined_rooms(),
                    name=f"matrix_rtc_reconcile_{self.agent_name}",
                    owner=self._runtime_view,
                )

    async def _on_sync_error(self, _response: nio.SyncError) -> None:
        """Update the watchdog clock on sync errors without marking cache state fresh."""
        logger.debug("SyncError received", agent_name=self.agent_name, error=str(_response))
        self._last_sync_monotonic = time.monotonic()
        if isinstance(_response, nio.SlidingSyncError):
            # nio restarts expired sliding connections (M_UNKNOWN_POS)
            # transparently, and sliding errors say nothing about the classic
            # sync checkpoint, so classic token rejection must not run here.
            self._warn_if_sliding_sync_never_succeeded(_response)
            return
        if _response.status_code == "M_UNKNOWN_POS":
            decision = await self._sync_checkpoint_trust.reject_unknown_pos()
            await self._apply_client_rewind_decision(decision)
            self._room_member_join_hooks_armed = False
            self.logger.warning(
                "matrix_sync_token_rejected",
                status_code=_response.status_code,
                error=str(_response),
                first_sync=not self._first_sync_done,
            )

    def _warn_if_sliding_sync_never_succeeded(self, response: nio.SlidingSyncError) -> None:
        """Point at the classic transport once when sliding sync fails before ever succeeding."""
        if self._first_sync_done or self._sliding_sync_startup_warning_emitted:
            return
        self._sliding_sync_startup_warning_emitted = True
        self.logger.warning(
            "sliding_sync_failing_before_first_sync",
            status_code=response.status_code,
            error=str(response),
            hint=(
                "If the homeserver does not support MSC4186 Simplified Sliding Sync"
                " (org.matrix.simplified_msc3575), set matrix_sync.mode: classic."
            ),
        )

    async def ensure_rooms(self) -> None:
        """Ensure agent is in the correct rooms based on configuration.

        This consolidates room management into a single method that:
        1. Joins configured rooms
        2. Leaves unconfigured rooms
        """
        await self.join_configured_rooms()
        await self.leave_unconfigured_rooms()

    def _register_call_manager_callbacks(self, client: nio.AsyncClient) -> None:
        """Build the optional call manager and wire its Matrix callbacks."""
        self._call_manager = maybe_build_call_manager(
            agent_name=self.agent_name,
            config=self.config,
            client=client,
            runtime_paths=self.runtime_paths,
            ssl_verify=constants.runtime_matrix_ssl_verify(self.runtime_paths),
            tool_support=self._tool_runtime_support,
            get_invited_rooms_by_agent=self._invited_call_rooms_by_agent,
        )

        client.add_event_callback(
            _create_best_effort_task_wrapper(
                self._on_room_membership_event,
                owner=self._runtime_view,
                admit=self._admit_live_call_event,
            ),
            nio.RoomMemberEvent,
        )
        call_manager = self._call_manager
        if call_manager is None:
            return

        client.add_event_callback(
            _create_best_effort_task_wrapper(
                call_manager.on_room_event,
                owner=self._runtime_view,
                admit=self._admit_live_call_event,
            ),
            nio.UnknownEvent,
        )
        client.add_to_device_callback(
            _create_best_effort_task_wrapper(  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
                call_manager.on_to_device_event,
                owner=self._runtime_view,
            ),
            AuthenticatedToDeviceEvent,
        )

    def _admit_live_call_event(self, _room: nio.MatrixRoom, event: nio.Event) -> bool:
        """Admit call-runtime room state only for this live nio delivery."""
        return journal_event_is_live(event.event_id)

    async def _apply_own_room_membership_from_sync(self, response: nio.SyncResponse) -> None:
        """Apply this bot's authoritative joined/left room sections before other sync work."""
        await self._apply_own_room_membership(
            own_membership_from_sync(response, self_user_id=self.agent_user.user_id),
        )

    async def _apply_own_room_membership_from_sliding_sync(self, response: nio.SlidingSyncResponse) -> None:
        """Apply this bot's room memberships reported by one sliding sync response."""
        await self._apply_own_room_membership(
            own_membership_from_sliding_sync(response, self_user_id=self.agent_user.user_id),
        )

    async def _apply_own_room_membership(self, membership: OwnRoomMembership) -> None:
        """Fence departed rooms and report current membership for one sync response."""
        await self._membership_fence.fence_reported_departures(membership.departures)
        departed_room_ids = membership.departed_room_ids
        for room_id in departed_room_ids:
            self._room_lifecycle.forget_invited_room(room_id)
        self._local_departures_awaiting_sync.difference_update(departed_room_ids)
        current_joined_room_ids = (
            membership.joined_room_ids - membership.left_room_ids - self._local_departures_awaiting_sync
        )
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_sync_room_membership(
                joined_room_ids=current_joined_room_ids,
                left_room_ids=membership.left_room_ids,
            )

    def _invited_call_rooms_by_agent(self) -> dict[str, frozenset[str]]:
        """Return live accepted-invite state for the configured call agents."""
        orchestrator = self.orchestrator
        agent_bots_value = orchestrator.agent_bots if orchestrator is not None else None
        if not isinstance(agent_bots_value, dict):
            return {self.agent_name: frozenset(self._room_lifecycle.invited_rooms)}
        agent_bots = cast("dict[str, object]", agent_bots_value)

        invited_rooms: dict[str, frozenset[str]] = {}
        for agent_name in self.config.calls.agents:
            bot = agent_bots.get(agent_name)
            if isinstance(bot, AgentBot):
                invited_rooms[agent_name] = frozenset(bot._room_lifecycle.invited_rooms)
        return invited_rooms

    async def _on_room_membership_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMemberEvent,
    ) -> None:
        """Apply invited-room cleanup before optional call reconciliation."""
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_room_membership_event(room, event)

    async def start(self) -> None:
        """Start the agent bot with user account setup (but don't join rooms yet)."""
        await self.ensure_user_account()
        matrix_id_before_login = self.matrix_id
        client = await login_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
            sync_storage=MatrixSyncStorage(
                store_tokens=False,
                persist_recovery=self.config.matrix_sync.mode == "sliding",
            ),
        )
        self.client = client
        # Captured the moment it becomes true. A restart that logs in as a new
        # device must not resend under the old device's transaction IDs
        # believing they still deduplicate.
        self._sending_device_id = client.device_id or None
        try:
            self._rebuild_runtime_components_after_login_if_identity_changed(matrix_id_before_login)
            orchestrator = self.orchestrator
            if orchestrator is not None:
                orchestrator.validate_managed_entity_identities()
            self._runtime_view.mark_runtime_started()
            await self._prepare_matrix_sync_continuity()
            await self._room_lifecycle.restore_pending_join_decrypt_fences()
            await self._set_avatar_if_available()
            # Keep durable tracking-state loading off the event loop at startup.
            await self._turn_store.warm()
            await asyncio.to_thread(interactive.init_persistence, self.runtime_paths.storage_root)
            client = self.client
            assert client is not None

            # Persist correctness-critical source events; keep ID-less auxiliary inputs best-effort.
            client.add_event_callback(
                self._on_invite_before_sync_certification,  # ty: ignore[invalid-argument-type]
                nio.InviteEvent,  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
            )
            self._journal_dispatcher.register(client)
            self._register_call_manager_callbacks(client)
            register_desktop_pairing_receiver(
                self.config,
                client=client,
                agent_name=self.agent_name,
                runtime_paths=self.runtime_paths,
                callback_wrapper=lambda callback: _create_best_effort_task_wrapper(
                    callback,
                    owner=self._runtime_view,
                ),
            )
            await self._set_presence_with_model_info()
            client.add_response_callback(self._on_sync_response, (nio.SyncResponse, nio.SlidingSyncResponse))  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
            client.add_response_callback(self._on_sync_error, nio.SyncError)  # ty: ignore[invalid-argument-type]

            self.running = True

            # Router bot has additional responsibilities
            if self.agent_name == ROUTER_AGENT_NAME:
                try:
                    await cleanup_all_orphaned_bots(client, self.config, self.runtime_paths)
                except Exception as e:
                    self.logger.warning("orphaned_bot_cleanup_failed", error=str(e))

            # Note: Room joining is deferred until after invitations are handled
            self.logger.info("agent_setup_complete", user_id=self.agent_user.user_id)
            await self._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)
            self._journal_dispatcher.start()
        except Exception:
            client = self.client
            self.running = False
            self.client = None
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    self.logger.warning("Failed to close Matrix client after startup failure", exc_info=True)
            raise

    async def recover_pending_turn_journal_events(self) -> None:
        """Release fleet-dependent turn replay after the responder startup pass."""
        self._journal_dispatcher.release_turn_replay()
        await self._journal_dispatcher.drain_once()
        await self._turn_store.cleanup(
            unsettled_source_event_ids=await self._journal_dispatcher.unsettled_event_ids(),
        )

    async def try_start(self) -> bool:
        """Try to start the agent bot with smart retry logic.

        Retries transient failures but stops immediately on permanent startup errors.

        Returns:
            True if the bot started successfully, False otherwise.

        """

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_not_exception_type(PermanentStartupError),
            reraise=True,
        )
        async def _start_with_retry() -> None:
            await self.start()

        try:
            await _start_with_retry()
            return True  # noqa: TRY300
        except Exception as exc:
            if isinstance(exc, PermanentStartupError):
                logger.error("agent_start_failed_permanently", agent=self.agent_name, error=str(exc))  # noqa: TRY400
                raise
            logger.exception("agent_start_failed", agent=self.agent_name)
            return False

    async def cleanup(self) -> None:
        """Clean up the agent by leaving all rooms and stopping.

        This method ensures clean shutdown when an agent is removed from config.
        """
        assert self.client is not None
        # Leave all rooms (preserving DM rooms)
        try:
            joined_rooms = await get_joined_rooms(self.client)
            if joined_rooms:
                await leave_non_dm_rooms(
                    self.client,
                    joined_rooms,
                    on_room_left=self._fence_left_room,
                )
        except Exception:
            self.logger.exception("Error leaving rooms during cleanup")

        # Stop the bot
        await self.stop(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)

    async def _fence_left_room(self, room_id: str) -> None:
        """Fence one room immediately after this bot leaves it."""
        self._local_departures_awaiting_sync.add(room_id)
        await self._membership_fence.fence_local_departure(room_id)

    async def stop(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        """Stop the agent bot."""
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._classic_sync_rebuild_pending = False
        self._classic_sync_rebuild_attempt = 0
        self._orchestrator_ready_handled = False
        self._room_member_join_hooks_armed = False
        self._room_member_callback_registered = False
        clear_matrix_sync_state(self.agent_name)
        await self._emit_agent_lifecycle_event(EVENT_AGENT_STOPPED, stop_reason=shutdown_intent.stop_reason)

        call_manager = self._call_manager
        self._call_manager = None
        self._calls_reconcile_pending = False
        if call_manager is not None:
            await call_manager.shutdown()

        await self.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)

        if self.agent_name == ROUTER_AGENT_NAME:
            cleared_queued_tasks = clear_deferred_overdue_tasks()
            if cleared_queued_tasks > 0:
                self.logger.info("Cleared queued overdue scheduled tasks", count=cleared_queued_tasks)
            cancelled_tasks = await cancel_all_running_scheduled_tasks()
            if cancelled_tasks > 0:
                self.logger.info("Cancelled running scheduled tasks", count=cancelled_tasks)

        # Each of these owns a resource this bot alone holds, and none of them
        # may skip the others. A lane that already faulted makes dispatcher stop
        # raise, and before this isolation that exception skipped the store and
        # the client and then aborted the config reload's removal of this
        # generation -- leaving it registered, half-stopped, while its
        # replacement opened the same database under the same principal.
        failures: list[Exception] = []
        await self._release("journal dispatcher", self._journal_dispatcher.stop(), failures)
        if self._own_journal is not None:
            await self._release("journal store", self._own_journal.close(), failures)
        if self.client is not None:
            self.logger.warning("Client is not None in stop()")
            await self._release("matrix client", self.client.close(), failures)
        if failures:
            # Every step ran, and the caller still has to hear about it.
            # `stop_entities` pops from the runtime map only after its gather
            # returns, so raising keeps this bot registered and stops the reload
            # before it creates a replacement on a store that never closed.
            # Swallowing is what would certify a partial stop as a clean one.
            raise failures[0]
        self.logger.info("Stopped agent bot")

    async def _release(self, what: str, closing: Awaitable[None], failures: list[Exception]) -> None:
        """Await one shutdown step, recording failure so the later steps still run."""
        try:
            await closing
        except Exception as error:
            self.logger.exception("Failed to release resource during stop", resource=what)
            failures.append(error)

    async def _send_welcome_message_if_empty(self, room_id: str, visible_to_sender_id: str | None = None) -> None:
        """Send a welcome message if the room has no messages yet.

        Only called by the router agent when joining a room.
        """
        await self._room_lifecycle.send_welcome_message_if_empty(room_id, visible_to_sender_id)

    def _maybe_start_deferred_overdue_task_drain(self) -> None:
        """Start draining queued overdue tasks once Matrix sync is ready."""
        if self.agent_name != ROUTER_AGENT_NAME or self.client is None or self._sync_shutting_down:
            return

        existing_task = self._deferred_overdue_task_drain_task
        if existing_task is not None and not existing_task.done():
            return

        self._deferred_overdue_task_drain_task = asyncio.create_task(
            self._drain_deferred_overdue_task_queue(),
            name=f"deferred_overdue_task_drain_{self.agent_name}",
        )

    async def _drain_deferred_overdue_task_queue(self) -> None:
        """Drain queued overdue tasks without blocking sync callbacks."""
        assert self.client is not None

        try:
            drained_count = await drain_deferred_overdue_tasks(
                self.client,
                self.config,
                self.runtime_paths,
                self._conversation_reader,
            )
            if drained_count > 0:
                self.logger.info("Started deferred overdue scheduled tasks", count=drained_count)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Failed to drain deferred overdue scheduled tasks")

    async def _cancel_deferred_overdue_task_drain(self) -> None:
        """Cancel the background overdue-task drain task if one exists."""
        drain_task = self._deferred_overdue_task_drain_task
        self._deferred_overdue_task_drain_task = None
        if drain_task is None:
            return

        if not drain_task.done():
            drain_task.cancel()

        await asyncio.gather(drain_task, return_exceptions=True)

    async def prepare_for_sync_shutdown(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        """Cancel work that must not outlive the Matrix sync loop."""
        if not self._sync_shutting_down:
            self.logger.info(
                "matrix_agent_response_runtime_shutdown",
                active_response_count=self.in_flight_response_count,
                restart_reason_category=restart_reason_category_for(shutdown_intent),
                resulting_action="drain_then_cancel_response_runtime",
            )
        self._sync_shutting_down = True
        self._delivery_recovery_wake.set()
        self._response_runner.refuse_pending_admissions()
        if self.agent_name == ROUTER_AGENT_NAME:
            await self._cancel_deferred_overdue_task_drain()
        background_tasks_completed = await wait_for_background_tasks(
            timeout=5.0,
            owner=self._runtime_view,
            shutdown_intent=shutdown_intent,
        )
        drain_result = await self._coalescing_gate.drain_all(
            ready_timeout_seconds=5.0,
            shutdown_intent=shutdown_intent,
        )
        responses_drained = await self._response_runner.drain_inbox_responses(
            cancel_after_seconds=5.0,
            shutdown_intent=shutdown_intent,
        )
        pending_response_count = self._response_runner.pending_inbox_response_count
        if not responses_drained:
            self.logger.warning(
                "matrix_agent_response_drain_incomplete",
                agent_name=self.agent_name,
                active_response_count=self.in_flight_response_count,
                pending_response_count=pending_response_count,
                response_recovery_complete=self._response_runner.incomplete_inbox_responses_recoverable,
                restart_reason_category=restart_reason_category_for(shutdown_intent),
            )
        post_drain_background_tasks_completed = await wait_for_background_tasks(
            timeout=5.0,
            owner=self._runtime_view,
            shutdown_intent=shutdown_intent,
        )
        if self._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED:
            await self._sync_checkpoint_trust.persist_current()
        if (
            not background_tasks_completed
            or not drain_result.completed
            or not responses_drained
            or not post_drain_background_tasks_completed
        ):
            self.logger.warning(
                "runtime_drain_incomplete_with_durable_dispatch_recovery",
                agent_name=self.agent_name,
                background_tasks_completed=background_tasks_completed,
                coalescing_drain_completed=drain_result.completed,
                responses_drained=responses_drained,
                response_recovery_complete=self._response_runner.incomplete_inbox_responses_recoverable,
                post_drain_background_tasks_completed=post_drain_background_tasks_completed,
                released_reservation_count=drain_result.released_reservation_count,
                cancelled_unready_count=drain_result.cancelled_unready_count,
                failed_ready_count=drain_result.failed_ready_count,
                dropped_ready_count=drain_result.dropped_ready_count,
                dispatch_failure_count=drain_result.dispatch_failure_count,
                dispatch_cancelled_count=drain_result.dispatch_cancelled_count,
            )

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        while True:
            try:
                await run_matrix_sync_forever(
                    self.client,
                    config=self.config,
                    agent_name=self.agent_name,
                    room_ids=self.rooms,
                    timeout_ms=_SYNC_TIMEOUT_MS,
                    sync_filter=_SYNC_FILTER,
                    first_sync_done=self._first_sync_done and not self._classic_sync_rebuild_pending,
                )
            finally:
                await self._reconcile_classic_sync_cursor_after_loop_exit()
            if not (self.config.matrix_sync.mode == "classic" and self._classic_sync_rebuild_pending and self.running):
                return
            retry_delay = _classic_sync_rebuild_backoff_seconds(self._classic_sync_rebuild_attempt)
            if retry_delay > 0:
                self.logger.warning(
                    "matrix_sync_rebuild_retry_backoff",
                    attempt=self._classic_sync_rebuild_attempt,
                    retry_in_seconds=retry_delay,
                )
                await asyncio.sleep(retry_delay)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        await self._room_lifecycle.on_invite(room, event)

    async def _on_invite_before_sync_certification(
        self,
        room: nio.MatrixRoom,
        event: nio.InviteEvent,
    ) -> None:
        """Act on one invite without journalling it.

        An invite has no Matrix event ID to key durable work on, and it does
        not need one: an invite the bot has not acted on reappears in every
        sync response until it does, so the homeserver already provides the
        redelivery a journal row would have.
        """
        create_background_task(self._on_invite(room, event), owner=self._runtime_view)

    def _room_for_journal_event(self, room_id: str) -> nio.MatrixRoom:
        """Resolve one recovery room without depending on a new sync response."""
        client = self.client
        if client is not None and room_id in client.rooms:
            return client.rooms[room_id]
        return nio.MatrixRoom(room_id, self.matrix_id.full_id)

    async def _dispatch_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Delegate one flushed coalesced batch to the turn engine."""
        await self._turn_controller.handle_coalesced_batch(batch)

    def _retry_failed_coalesced_dispatch(self, pending_events: tuple[PendingEvent, ...]) -> None:
        """Return failed gate sources to their exact durable callback owner."""
        for pending_event in pending_events:
            self._retry_pending_dispatch_source(pending_event.event.event_id)

    def _retry_pending_dispatch_source(self, source_event_id: str) -> None:
        """Return one undelivered source to its exact durable callback owner."""
        self._journal_dispatcher.retry_turn_source(source_event_id)

    async def _settle_ignored_dispatch_source(self, source_event_id: str) -> None:
        """Settle one asynchronously normalized source that produced no dispatch payload."""
        await self._journal_dispatcher.settle_intentionally_ignored_turn_sources(
            (source_event_id,),
        )

    def _log_matrix_event_callback_started(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted | MatrixMediaEvent,
        *,
        callback_name: str,
    ) -> None:
        """Log Matrix ingress timing without message content."""
        receive_timestamp_ms = int(time.time() * 1000)
        origin_server_ts = origin_server_ts_from_event_source(event.source)
        log_context: dict[str, object] = {
            "callback": callback_name,
            "event_id": event.event_id,
            "room_id": room.room_id,
            "agent_name": self.agent_name,
            "receive_timestamp_ms": receive_timestamp_ms,
        }
        if origin_server_ts is not None:
            log_context["origin_server_ts_ms"] = origin_server_ts
            log_context["matrix_event_receive_lag_ms"] = round(receive_timestamp_ms - float(origin_server_ts), 1)
        self.logger.info("matrix_event_callback_started", **log_context)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        """Delegate one inbound text event to the turn engine.

        Every `m.room.message` the journal admitted as work reaches here, which
        is `m.text` and `m.emote`. An emote is a user utterance whose body is
        written in the third person, so it needs no special handling: the same
        mentions, commands, and routing apply to `/me asks the bot to X` as to
        the sentence typed without the `/me`.
        """
        receipt_time = time.monotonic()
        self._log_matrix_event_callback_started(room, event, callback_name="message")
        semantic_consumer = self._journal_dispatcher.semantic_consumer()
        approval_reply_claimed = semantic_consumer is SemanticConsumer.APPROVAL_REPLY

        async def claim_approval_reply() -> None:
            nonlocal approval_reply_claimed
            await self._journal_dispatcher.claim_semantic_consumer(
                SemanticConsumer.APPROVAL_REPLY,
            )
            approval_reply_claimed = True

        early_reservation_owner = None
        approval_reply_to_event_id = EventInfo.from_event(event.source).reply_to_event_id
        if approval_reply_to_event_id is not None and is_process_active_approval_card(approval_reply_to_event_id):
            requester_user_id = self._ingress_validator.requester_user_id(
                sender=event.sender,
                source=event.source,
            )
            early_reservation_owner = self._turn_controller.reserve_prompt_ingress_order(
                room,
                requester_user_id,
                receipt_time=receipt_time,
            )
        try:
            approval_reply_handled = await maybe_handle_tool_approval_reply(
                room=room,
                event=event,
                config=self.config,
                runtime_paths=self.runtime_paths,
                orchestrator=self.orchestrator,
                logger=self.logger,
                before_consume=None if approval_reply_claimed else claim_approval_reply,
                authorization_prevalidated=approval_reply_claimed,
            )
            if approval_reply_claimed or approval_reply_handled:
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED
            return await self._turn_controller.handle_text_event(
                room,
                event,
                receipt_time=receipt_time,
                reservation_owner=early_reservation_owner,
            )
        finally:
            if early_reservation_owner is not None:
                await early_reservation_owner.release()

    async def _on_redaction(self, _room: nio.MatrixRoom, event: nio.Event) -> None:
        """Tombstone the redacted source so no replay reruns the turn it started.

        The projection learns about the redaction through journal admission, so
        this owes only the durable tombstone. Raising leaves the callback
        unaccepted and the source available for sync to redeliver.
        """
        assert isinstance(event, nio.RedactionEvent)
        await self._turn_store.mark_source_redacted(event.redacts)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_lookup_scope():
            await self._reaction_dispatcher.dispatch(room, event)

    async def _on_room_member(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMemberEvent,
    ) -> None:
        """Expose live human room joins to router-owned hooks."""
        if self.agent_name != ROUTER_AGENT_NAME:
            return
        if not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            return

        await emit_room_member_join_at_least_once(
            room,
            event,
            config=self.config,
            runtime_paths=self.runtime_paths,
            storage_root=self.runtime_paths.storage_root,
            lock=self._room_member_join_lock,
            emit=self._emit_room_member_joined_hooks,
        )

    async def _emit_room_member_joined_sync_state_hooks(
        self,
        response: nio.SyncResponse,
        *,
        record_only: bool = False,
        include_timeline_baseline: bool = False,
        dispatch_snapshot_joins: bool = False,
    ) -> None:
        """Expose or record human joins that matrix-nio delivers through sync room state."""
        if self.agent_name != ROUTER_AGENT_NAME:
            return
        if dispatch_snapshot_joins and not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            record_only = True
            dispatch_snapshot_joins = False
        elif not record_only and not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            return
        client = self.client
        if client is None:
            return

        plan = room_member_sync_state_plan(
            response,
            rooms=client.rooms,
            config=self.config,
            runtime_paths=self.runtime_paths,
            record_only=record_only,
            include_timeline_baseline=include_timeline_baseline,
            dispatch_snapshot_joins=dispatch_snapshot_joins,
        )
        for room, event in plan.dispatch_events:
            await self._journal_dispatcher.admit_and_run(
                room,
                event,
                EventKind.ROOM_LIFECYCLE,
                EventClass.ACTIONABLE,
            )
        if plan.record_events:
            unsettled_members = await self._journal_dispatcher.unsettled_room_lifecycle_member_ids()
            record_events = tuple(
                (room, event)
                for room, event in plan.record_events
                if (room.room_id, event.state_key) not in unsettled_members
            )
        else:
            record_events = ()
        if record_events:
            async with self._room_member_join_lock:
                await asyncio.to_thread(
                    record_room_member_joins_seen_from_events,
                    record_events,
                    config=self.config,
                    runtime_paths=self.runtime_paths,
                    storage_root=self.runtime_paths.storage_root,
                )

    async def _emit_room_member_joined_sync_timeline_hooks(self, response: nio.SyncResponse) -> None:
        """Expose human joins from a restored-token catch-up sync timeline."""
        if self.agent_name != ROUTER_AGENT_NAME:
            return
        if not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            return
        client = self.client
        if client is None:
            return

        for room, event in room_member_sync_timeline_events(
            response,
            rooms=client.rooms,
            config=self.config,
            runtime_paths=self.runtime_paths,
        ):
            event_class = self._journal_dispatcher.timeline_member_event_class(event)
            if event_class is None:
                # nio accepted this event on an earlier pass and so said
                # nothing about it in this response. It is already journaled
                # with its true class, and a guess here would settle a
                # recovered join against it, permanently.
                continue
            await self._journal_dispatcher.admit_and_run(
                room,
                event,
                EventKind.ROOM_LIFECYCLE,
                event_class,
            )

    async def _on_decryption_failure(self, room: nio.MatrixRoom, event: nio.MegolmEvent) -> None:
        await self._handle_decryption_failure_event(
            room,
            event,
            suppress_notice=self._room_lifecycle.decrypt_notice_is_fenced(room.room_id),
        )

    def _delivered_turn_source_ids(self, turn_id: str) -> tuple[str, ...]:
        """Return every journal source one turn's answer discharges.

        The turn is named by its anchor event, which is what the outbox keys
        on, but a coalesced batch answers several sources at once and all of
        them are handed over together. Without the ledger's own record of that
        set, the anchor would settle and its siblings would replay into a turn
        that has already been answered.
        """
        record = self._turn_store.get_turn_record(turn_id)
        return record.indexed_event_ids if record is not None else (turn_id,)

    async def _handle_decryption_failure_event(
        self,
        room: nio.MatrixRoom,
        event: nio.MegolmEvent,
        *,
        suppress_notice: bool,
    ) -> None:
        """Apply authorization before encrypted-event recovery and visibility."""
        client = self.client
        assert client is not None
        if not is_authorized_sender(event.sender, self.config, room.room_id, self.runtime_paths):
            self.logger.debug(
                "ignoring_decrypt_failure_from_unauthorized_sender",
                user_id=event.sender,
                room_id=room.room_id,
            )
            return
        await handle_decrypt_failure(
            client,
            room,
            event,
            agent_name=self.agent_name,
            runtime_paths=self.runtime_paths,
            suppress_notice=suppress_notice,
        )

    async def _on_unknown_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Handle custom Matrix events that are not part of nio's typed event set."""
        if event.type != "io.mindroom.tool_approval_response":
            return
        raw_sender_id = event.source.get("sender")
        if not isinstance(raw_sender_id, str) or not raw_sender_id:
            self.logger.debug("ignoring_tool_approval_response_without_sender")
            return
        payload = parse_approval_response_event(event)
        if payload.status is None or (payload.card_event_id is None and payload.approval_id is None):
            return
        await handle_tool_approval_action(
            room=room,
            sender_id=raw_sender_id,
            config=self.config,
            runtime_paths=self.runtime_paths,
            orchestrator=self.orchestrator,
            logger=self.logger,
            approval_event_id=payload.card_event_id,
            approval_id=payload.approval_id,
            status=payload.status,
            reason=payload.reason,
        )

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
    ) -> TurnDispatchOutcome:
        """Delegate one inbound media event to the turn engine."""
        receipt_time = time.monotonic()
        self._log_matrix_event_callback_started(room, event, callback_name="media")
        return await self._turn_controller.handle_media_event(room, event, receipt_time=receipt_time)

    async def _run_regenerated_response(self, request: ResponseRequest) -> str | None:
        """Run one edit-regenerated turn through this bot's response path."""
        return await self._response_runner.generate_response(request)

    async def _hook_send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None = None,
        *,
        trigger_dispatch: bool = False,
    ) -> _MatrixEventId | None:
        """Send a hook-originated Matrix message with stable metadata tags."""
        if self.client is None:
            self.logger.warning("Hook send requested before Matrix client is ready", room_id=room_id)
            return None

        event_id = await send_hook_message(
            self.client,
            self.config,
            self.runtime_paths,
            room_id,
            body,
            thread_id,
            source_hook,
            extra_content,
            trigger_dispatch=trigger_dispatch,
            conversation_reader=self._conversation_reader,
        )
        if event_id:
            self.logger.info("Sent hook message", event_id=event_id, room_id=room_id, source_hook=source_hook)
            return event_id
        self.logger.error("Failed to send hook message", room_id=room_id, source_hook=source_hook)
        return None

    async def _hook_agent_message_snapshot(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
    ) -> AgentMessageSnapshot | None:
        """Read the latest visible sender message for hook helpers."""
        return await latest_agent_message_snapshot(
            self._conversation_reader,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
        )

    async def _redact_message_event(
        self,
        *,
        room_id: str,
        event_id: str,
        reason: str,
    ) -> bool:
        """Redact one visible event when a provisional response should disappear entirely."""
        if self.client is None:
            return False
        response = await self.client.room_redact(room_id, event_id, reason=reason)
        if isinstance(response, nio.RoomRedactError):
            self.logger.error("Failed to redact message", event_id=event_id, error=str(response))
            return False
        return True


class TeamBot(AgentBot):
    """A bot that represents a team of agents working together."""

    # Team configuration
    team_mode: str
    team_model: str | None

    def __init__(
        self,
        agent_user: AgentMatrixUser,
        storage_path: Path,
        config: Config,
        runtime_paths: RuntimePaths,
        rooms: list[str] | None = None,
        config_path: Path | None = None,
        *,
        team_mode: str = "coordinate",
        team_model: str | None = None,
        enable_streaming: bool = True,
        journal_store: EventJournalStore | None = None,
    ) -> None:
        """Initialize the team bot and its shared agent runtime."""
        super().__init__(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths,
            rooms=rooms,
            config_path=config_path,
            enable_streaming=enable_streaming,
            journal_store=journal_store,
        )
        self.team_mode = team_mode
        self.team_model = team_model

    @cached_property
    def agent(self) -> Agent | None:
        """Teams don't have individual agents, return None."""
        return None

    def current_configured_team_agents(self) -> list[MatrixID]:
        """Return this configured team's current persisted member Matrix IDs."""
        team_config = self.config.teams[self.agent_name]
        registry = entity_identity_registry(self.config, self.runtime_paths)
        return [registry.current_id(agent_name) for agent_name in team_config.agents]

    async def _run_regenerated_response(self, request: ResponseRequest) -> str | None:
        """Run one edit-regenerated turn through the configured team path."""
        target = request.response_envelope.target
        if not request.prompt.strip():
            return await self._response_runner.generate_response_for_empty_prompt(
                request,
                response_kind="team",
            )
        assert self.client is not None
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                model_prompt=request.model_prompt,
            )
        )

        configured_mode = TeamMode.COORDINATE if self.team_mode == "coordinate" else TeamMode.COLLABORATE
        availability = self._turn_policy.responder_availability()
        team_resolution = resolve_configured_team(
            self.agent_name,
            self.current_configured_team_agents(),
            configured_mode,
            self.config,
            self.runtime_paths,
            materializable_agent_names=availability.materializable_agent_names,
        )
        if team_resolution.outcome is not TeamOutcome.TEAM:
            assert team_resolution.reason is not None
            return await self._response_runner.generate_team_response_helper(
                request,
                team_agents=self.current_configured_team_agents(),
                team_mode=configured_mode.value,
                resolution_reason=team_resolution.reason,
            )
        assert team_resolution.mode is not None

        registry = entity_identity_registry(self.config, self.runtime_paths)
        agent_names = [
            registry.current_entity_name_for_user_id(mid.full_id, include_router=False) or mid.username
            for mid in team_resolution.eligible_members
        ]
        session_id = target.session_id
        execution_identity = self._tool_runtime_support.build_execution_identity(
            target=target,
            user_id=request.user_id,
        )
        if request.sync_restart_retry_source_event_id is None:
            with tool_execution_identity(execution_identity):
                create_background_task(
                    store_conversation_memory(
                        memory_prompt,
                        agent_names,
                        self.storage_path,
                        session_id,
                        self.config,
                        self.runtime_paths,
                        memory_thread_history,
                        request.user_id,
                        execution_identity=execution_identity,
                    ),
                    name=f"memory_save_team_{session_id}",
                    owner=self._runtime_view,
                )

        return await self._response_runner.generate_team_response_helper(
            replace(
                request,
                prompt=memory_prompt,
                model_prompt=model_prompt_text,
                thread_history=model_thread_history,
                media=request.media or MediaInputs(),
                user_id=request.user_id or "",
                correlation_id=request.correlation_id or target.reply_to_event_id,
            ),
            team_agents=team_resolution.eligible_members,
            team_mode=team_resolution.mode.value,
            reason_prefix=f"Team '{self.agent_name}'",
        )
