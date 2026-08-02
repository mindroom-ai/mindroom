"""Matrix runtime shell for agents, teams, and the router."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import nio
from tenacity import before_sleep_log, retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.approval_inbound import (
    handle_tool_approval_action,
    maybe_handle_tool_approval_reply,
    parse_approval_response_event,
)
from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.desktop.pairing_receiver import register_desktop_pairing_receiver
from mindroom.entity_resolution import entity_identity_registry
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
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.decrypt_failure import handle_decrypt_failure, raise_notice_floor
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
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCertificationDecision,
    SyncTrustState,
)
from mindroom.matrix.sync_loop import run_matrix_sync_forever, sliding_own_membership_sets
from mindroom.matrix.users import AgentMatrixUser, login_agent_user
from mindroom.matrix_rtc.call_manager import CallManager, maybe_build_call_manager
from mindroom.memory import store_conversation_memory
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.post_response_effects import PostResponseEffectsSupport
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
from .dispatch_obligations import (
    DispatchCallbackKind,
    DispatchObligationRunner,
    DispatchObligationStore,
    DispatchSemanticConsumer,
    callback_kind_for_source_kind,
)
from .edit_regenerator import EditRegenerator, EditRegeneratorDeps
from .entity_rooms import get_rooms_for_entity
from .inbound_turn_normalizer import InboundTurnNormalizer, InboundTurnNormalizerDeps
from .ingress_validation import IngressValidator, IngressValidatorDeps
from .knowledge import KnowledgeAccessSupport
from .logging_config import get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client_room_admin import get_joined_rooms
from .matrix.client_session import PermanentMatrixStartupError
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
from .redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps
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
from .turn_settlement_retry import TurnSettlementRetry
from .turn_store import TurnStore, TurnStoreDeps
from .user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from .visible_response_reconciliation import VisibleResponseReconciler, VisibleResponseReconcilerDeps
from .visible_voice_echo import VisibleVoiceEchoDeps, VisibleVoiceEchoLifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from pathlib import Path

    import structlog
    from agno.agent import Agent

    from mindroom.coalescing_batch import CoalescedBatch
    from mindroom.config.main import Config
    from mindroom.matrix.cache import AgentMessageSnapshot, ConversationEventCache, EventCacheWriteCoordinator
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.runtime_support import StartupThreadPrewarmRegistry

type _MatrixEventId = str

logger = get_logger(__name__)

__all__ = ["AgentBot", "TeamBot", "create_bot_for_entity"]


# Constants
_SYNC_TIMEOUT_MS = 30000
# Raise the per-room timeline limit above the homeserver default (~10) so a
# room has to flood much harder before the server truncates its timeline and
# forces a limited-sync gap backfill. This only widens the timeline window; it
# leaves every other section at server defaults so no event type is filtered
# out.
_SYNC_TIMELINE_LIMIT = 50
_SYNC_FILTER: dict[str, object] = {"room": {"timeline": {"limit": _SYNC_TIMELINE_LIMIT}}}


@dataclass(frozen=True, slots=True)
class _RoomMemberJoinSyncHookPlan:
    """Room-member join hook actions derived from one sync response."""

    arm_after_response: bool = True
    emit_state: bool = False
    emit_timeline: bool = False
    record_state_seen: bool = False


def _create_best_effort_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    *,
    owner: BotRuntimeState | None = None,
) -> Callable[..., Awaitable[None]]:
    """Run one explicitly best-effort callback as a background task.

    Use this only for auxiliary consumers or Matrix inputs without a stable
    source event ID.
    Correctness-critical source-backed events use ``DispatchObligationRunner``.
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
    config: Config,
    runtime_paths: RuntimePaths,
    storage_path: Path,
    config_path: Path | None = None,
) -> AgentBot | TeamBot | None:
    """Create appropriate bot instance for an entity (agent, team, or router).

    Args:
        entity_name: Name of the entity to create a bot for
        agent_user: Matrix user for the bot
        config: Configuration object
        runtime_paths: Explicit runtime context for paths, env, and Matrix identity resolution
        storage_path: Path for storing agent data
        config_path: Path to the YAML config file used by config-aware tools

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
    _sync_shutting_down: bool

    # Shared runtime state and extracted collaborators
    _hook_registry_state: HookRegistryState
    _runtime_view: BotRuntimeState
    _coalescing_gate: CoalescingGate
    _inbound_turn_normalizer: InboundTurnNormalizer
    _turn_policy: TurnPolicy
    _conversation_resolver: ConversationResolver
    _conversation_state_writer: ConversationStateWriter
    _conversation_cache: MatrixConversationCache
    _delivery_gateway: DeliveryGateway
    _response_runner: ResponseRunner
    _redacted_turn_cleanup: RedactedTurnCleanup
    _turn_store: TurnStore
    _visible_voice_echo: VisibleVoiceEchoLifecycle
    _tool_runtime_support: ToolRuntimeSupport
    _post_response_effects_support: PostResponseEffectsSupport
    _ingress_hook_runner: IngressHookRunner
    _request_payload_preparer: ResponsePayloadPreparer
    _hook_context_support: HookContextSupport
    _knowledge_access_support: KnowledgeAccessSupport
    _deferred_overdue_task_drain_task: asyncio.Task[None] | None
    _startup_thread_prewarm_task: asyncio.Task[None] | None
    _call_manager: CallManager | None
    _calls_reconcile_pending: bool
    _room_member_callback_registered: bool
    _room_member_join_hooks_armed: bool
    _sliding_sync_startup_warning_emitted: bool
    _turn_controller: TurnController
    _room_lifecycle: BotRoomLifecycle
    _local_departures_awaiting_sync: set[str]
    _sync_cache_trust: SyncCacheTrust

    def __init__(
        self,
        agent_user: AgentMatrixUser,
        storage_path: Path,
        config: Config,
        runtime_paths: RuntimePaths,
        rooms: list[str] | None = None,
        config_path: Path | None = None,
        enable_streaming: bool = True,
    ) -> None:
        """Initialize the bot with canonical runtime-backed config state."""
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
        self._orchestrator_ready_handled = False
        self._sync_shutting_down = False
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
            event_cache=None,
            event_cache_write_coordinator=None,
            startup_thread_prewarm_registry=None,
        )
        self._sync_cache_trust = SyncCacheTrust(
            storage_path=self.storage_path,
            agent_name=self.agent_name,
            runtime=self._runtime_view,
            logger=self.logger,
        )
        self._deferred_overdue_task_drain_task = None
        self._startup_thread_prewarm_task = None
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
                get_logger=lambda: self.logger,
                get_configured_rooms=lambda: self.rooms,
                send_response=send_room_lifecycle_response,
                on_room_joined=self._on_room_joined,
                on_configured_room_joined=self._post_join_room_setup,
                on_room_left=self._purge_left_room,
            ),
        )
        self._init_runtime_components()

    def _init_runtime_components(self) -> None:
        """Initialize runtime-only helpers that depend on bound instance methods."""
        if not self.agent_user.user_id:
            msg = f"Missing Matrix ID for {self.agent_name!r} during runtime initialization"
            raise PermanentMatrixStartupError(msg)
        runtime_matrix_id = self.matrix_id
        self._dispatch_obligation_store = DispatchObligationStore(
            tracking_path=self.storage_path / "tracking",
            principal_id=runtime_matrix_id.full_id,
            entity_name=self.agent_name,
        )
        self._turn_settlement_retry = TurnSettlementRetry(
            store=self._dispatch_obligation_store,
            background_task_owner=self._runtime_view,
        )
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
        self._conversation_cache = MatrixConversationCache(
            logger=self.logger,
            runtime=self._runtime_view,
        )
        self._conversation_state_writer = ConversationStateWriter(
            ConversationStateWriterDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
            ),
        )
        self._conversation_resolver = ConversationResolver(
            ConversationResolverDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                matrix_id=runtime_matrix_id,
                conversation_cache=self._conversation_cache,
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
                tracking_base_path=self.storage_path / "tracking",
                state_writer=self._conversation_state_writer,
                resolver=self._conversation_resolver,
                tool_runtime=self._tool_runtime_support,
                on_terminal_turn_persisted=self._turn_settlement_retry.retry,
            ),
        )
        self._dispatch_obligation_runner = DispatchObligationRunner(
            store=self._dispatch_obligation_store,
            callbacks=DispatchObligationRunner.callbacks_for(
                on_message=self._on_message,
                on_media=self._on_media_message,
                on_reaction=self._on_reaction,
                on_approval=self._on_unknown_event,
                on_invite=self._on_invite,
                on_room_lifecycle=self._on_room_member,
                on_redaction=self._on_redaction,
                on_decryption_failure=self._on_decryption_failure,
                source_has_live_owner=self._coalescing_gate.has_pending_source_event,
            ),
            room_for_id=self._room_for_dispatch_obligation,
            turn_is_terminal=self._turn_store.is_durably_handled,
            on_persist_failure=self._record_dispatch_persist_failure,
            background_task_owner=self._runtime_view,
            room_lifecycle_admission_enabled=lambda: (
                self.agent_name == ROUTER_AGENT_NAME and self._first_sync_done and self._room_member_join_hooks_armed
            ),
        )
        self._post_response_effects_support = PostResponseEffectsSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            delivery_gateway=self._delivery_gateway,
            conversation_cache=self._conversation_cache,
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
                receipt_order=self._dispatch_obligation_runner.receipt_order,
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
        self._redacted_turn_cleanup = RedactedTurnCleanup(
            RedactedTurnCleanupDeps(
                conversation_cache=self._conversation_cache,
                turn_store=self._turn_store,
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
                settle_ignored_sources=self._dispatch_obligation_runner.settle_intentionally_ignored_turn_sources,
            ),
        )
        self._command_turn_executor = CommandTurnExecutor(
            CommandTurnExecutorDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                normalizer=self._inbound_turn_normalizer,
                conversation_cache=self._conversation_cache,
                turn_policy=self._turn_policy,
                turn_store=self._turn_store,
                visible_responses=self._visible_responses,
                event_cache=lambda: self.event_cache,
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
                conversation_cache=self._conversation_cache,
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
                retry_dispatch_sources=self._dispatch_obligation_runner.retry_pending_turn_sources,
            ),
        )
        self._reaction_dispatcher = ReactionDispatcher(
            ReactionDispatcherDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                obligation_runner=self._dispatch_obligation_runner,
                turn_policy=self._turn_policy,
                turn_store=self._turn_store,
                stop_manager=self.stop_manager,
                conversation_cache=self._conversation_cache,
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
        self.event_cache = self.event_cache.for_principal(self.matrix_id.full_id)
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
    def event_cache(self) -> ConversationEventCache:
        """Return the configured Matrix event cache."""
        event_cache = self._runtime_view.event_cache
        if event_cache is None:
            msg = "Matrix event cache is not initialized for this bot runtime"
            raise RuntimeError(msg)
        return event_cache

    @event_cache.setter
    def event_cache(self, value: ConversationEventCache | None) -> None:
        """Update the configured Matrix event cache."""
        self._runtime_view.event_cache = value

    @property
    def event_cache_write_coordinator(self) -> EventCacheWriteCoordinator:
        """Return the configured Matrix event-cache write coordinator."""
        coordinator = self._runtime_view.event_cache_write_coordinator
        if coordinator is None:
            msg = "Matrix event-cache write coordinator is not initialized for this bot runtime"
            raise RuntimeError(msg)
        return coordinator

    @event_cache_write_coordinator.setter
    def event_cache_write_coordinator(self, value: EventCacheWriteCoordinator | None) -> None:
        """Update the configured Matrix event-cache write coordinator."""
        self._runtime_view.event_cache_write_coordinator = value

    @property
    def startup_thread_prewarm_registry(self) -> StartupThreadPrewarmRegistry:
        """Return the shared startup thread-prewarm room-claim registry."""
        registry = self._runtime_view.startup_thread_prewarm_registry
        if registry is None:
            msg = "Startup thread prewarm registry is not initialized for this bot runtime"
            raise RuntimeError(msg)
        return registry

    @startup_thread_prewarm_registry.setter
    def startup_thread_prewarm_registry(self, value: StartupThreadPrewarmRegistry | None) -> None:
        """Update the shared startup thread-prewarm room-claim registry."""
        self._runtime_view.startup_thread_prewarm_registry = value

    @property
    def runtime_started_at(self) -> float:
        """Return when this bot runtime started."""
        return self._runtime_view.runtime_started_at

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "agent_bot_latest_thread_event_lookup",
    ) -> str | None:
        """Return the latest event id for one Matrix thread when the cache knows it."""
        return await self._conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_id,
            caller_label=caller_label,
        )

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

    def _startup_thread_prewarm_enabled(self) -> bool:
        """Return whether this runtime entity should prewarm recent thread snapshots on startup."""
        if self.agent_name == ROUTER_AGENT_NAME:
            return self.config.router.startup_thread_prewarm
        if self.agent_name in self.config.teams:
            return self.config.teams[self.agent_name].startup_thread_prewarm
        return self.config.agents[self.agent_name].startup_thread_prewarm

    def _maybe_start_startup_thread_prewarm(self) -> None:
        """Start startup thread prewarm once the first sync is ready."""
        if self.client is None or self._sync_shutting_down or not self._startup_thread_prewarm_enabled():
            return

        existing_task = self._startup_thread_prewarm_task
        if existing_task is not None and not existing_task.done():
            return

        self._startup_thread_prewarm_task = create_background_task(
            self._run_startup_thread_prewarm(),
            name=f"startup_thread_prewarm_{self.agent_name}",
            owner=self._runtime_view,
        )

    async def _get_startup_thread_prewarm_joined_rooms(self) -> list[str]:
        """Return joined rooms for startup prewarm, failing open on lookup errors."""
        client = self.client
        assert client is not None
        try:
            joined_rooms = await get_joined_rooms(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._conversation_cache.logger.warning(
                "startup_thread_prewarm_joined_rooms_failed",
                error=str(exc),
            )
            return []
        return joined_rooms or []

    async def _prewarm_claimed_startup_thread_room(self, room_id: str) -> None:
        """Prewarm one claimed room and release the claim unless the room-level pass finishes."""
        completed = False
        try:
            async with self.startup_thread_prewarm_registry.room_slot():
                completed = await self._conversation_cache.prewarm_recent_room_threads(
                    room_id,
                    is_shutting_down=lambda: self._sync_shutting_down,
                )
        finally:
            if not completed:
                await self.startup_thread_prewarm_registry.release(self.event_cache.principal_id, room_id)

    async def _run_startup_thread_prewarm(self) -> None:
        """Prewarm recent thread snapshots with one bulk scan per joined room."""
        try:
            joined_rooms = await self._get_startup_thread_prewarm_joined_rooms()
            for room_id in joined_rooms:
                if self._sync_shutting_down:
                    return
                if not await self.startup_thread_prewarm_registry.try_claim(self.event_cache.principal_id, room_id):
                    continue
                await self._prewarm_claimed_startup_thread_room(room_id)
        finally:
            current_task = asyncio.current_task()
            if current_task is not None and self._startup_thread_prewarm_task is current_task:
                self._startup_thread_prewarm_task = None

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
                        caller_label="reaction_hook_context",
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
            self.event_cache,
            self._conversation_cache,
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
        """Reopen cache access after an explicit homeserver-confirmed join."""
        self._local_departures_awaiting_sync.discard(room_id)
        await self._conversation_cache.mark_room_joined(room_id)

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
        """Apply cache-trust startup output to the authenticated Matrix client."""
        client = self.client
        assert client is not None
        sync_token = await self._sync_cache_trust.prepare_startup()
        cast("Any", client).next_batch = sync_token
        if sync_token is None and client.user_id:
            # A cold sync replays history handled by an older device.
            raise_notice_floor(client.user_id)

    def _certify_sync_response(
        self,
        *,
        next_batch: str | None,
        cache_result: SyncCacheWriteResult,
        first_sync: bool,
    ) -> SyncCertificationDecision:
        """Apply cache certification and any requested Matrix client rewind."""
        decision = self._sync_cache_trust.certify_response(
            next_batch=next_batch,
            cache_result=cache_result,
            first_sync=first_sync,
        )
        if decision.reset_client_token and self.client is not None:
            cast("Any", self.client).next_batch = None
        return decision

    def _rewind_sync_after_pre_certification_failure(self) -> None:
        """Replay a classic sync that failed before its position was certified."""
        client = self.client
        if client is None:
            return
        retry_token = self._sync_cache_trust.retry_token()
        cast("Any", client).next_batch = retry_token
        self.logger.warning(
            "pre_certification_sync_side_effect_failed_replaying_sync",
            has_retry_token=retry_token is not None,
        )

    def _rewind_sync_if_token_advanced(self) -> None:
        """Rewind only when nio advanced beyond the last certified position."""
        client = self.client
        if client is not None and cast("Any", client).next_batch != self._sync_cache_trust.retry_token():
            self._rewind_sync_after_pre_certification_failure()

    def _record_dispatch_persist_failure(self) -> None:
        """Latch rejected source work until its containing response is rejected."""
        self._sync_cache_trust.record_dispatch_persist_failure()
        self._rewind_sync_if_token_advanced()

    def _handle_pre_certification_failure(self) -> None:
        """Rewind one failed response and consume any durable-acceptance rejection."""
        self._sync_cache_trust.reject_response_before_certification()
        self._rewind_sync_if_token_advanced()

    def _apply_sync_response_after_dispatch_acceptance(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult,
        room_member_join_hook_plan: _RoomMemberJoinSyncHookPlan,
    ) -> tuple[SyncCertificationDecision, _RoomMemberJoinSyncHookPlan, bool]:
        """Apply certification only when every source callback reached durable ownership."""
        applied, rejected = self._sync_cache_trust.apply_response_after_dispatch_acceptance(
            decision,
            cache_result=cache_result,
        )
        if rejected:
            self._rewind_sync_if_token_advanced()
            return decision, _RoomMemberJoinSyncHookPlan(arm_after_response=False), True
        if applied.reset_client_token and self.client is not None:
            cast("Any", self.client).next_batch = None
        if applied.reset_client_token:
            room_member_join_hook_plan = _RoomMemberJoinSyncHookPlan(arm_after_response=False)
        return applied, room_member_join_hook_plan, False

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
        """Run live join work only after its matching admission succeeds."""
        durable_callback = self._dispatch_obligation_runner.task_wrapper(
            DispatchCallbackKind.ROOM_LIFECYCLE,
            owner=self._runtime_view,
        )

        async def wrapper(room: nio.MatrixRoom, event: nio.Event) -> None:
            if not isinstance(event, nio.RoomMemberEvent):
                return
            await durable_callback(room, event)

        return wrapper

    def _room_member_join_sync_hook_plan(
        self,
        *,
        first_sync_response: bool,
        restored_token_first_sync_response: bool,
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
        return _RoomMemberJoinSyncHookPlan(
            arm_after_response=True,
            emit_state=emit_certified_state,
            emit_timeline=restored_token_first_sync_response,
            record_state_seen=decision.state is SyncTrustState.CERTIFIED and not emit_certified_state,
        )

    async def _run_pre_certification_sync_response_side_effects(
        self,
        response: nio.SyncResponse,
        *,
        room_member_join_hook_plan: _RoomMemberJoinSyncHookPlan,
    ) -> None:
        """Finish source-backed lifecycle work before certifying its sync position."""
        if room_member_join_hook_plan.record_state_seen:
            await self._emit_room_member_joined_sync_state_hooks(response, record_only=True)
        if room_member_join_hook_plan.emit_timeline:
            await self._emit_room_member_joined_sync_timeline_hooks(response)
        if room_member_join_hook_plan.emit_state:
            await self._emit_room_member_joined_sync_state_hooks(response)

    async def _run_sync_response_side_effects(
        self,
        *,
        first_sync_response: bool,
    ) -> None:
        """Run side effects that do not own raw sync checkpoint safety."""
        if first_sync_response:
            self._register_room_member_callback_after_initial_sync()
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)

        orchestrator = self.orchestrator
        if not self._orchestrator_ready_handled and orchestrator is not None:
            await orchestrator.handle_bot_ready(self)
            self._orchestrator_ready_handled = True

        if first_sync_response:
            self._maybe_start_startup_thread_prewarm()

        if first_sync_response or has_deferred_overdue_tasks():
            self._maybe_start_deferred_overdue_task_drain()

    async def _on_sync_response(self, _response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Track successful sync responses for health checks and watchdogs."""
        first_sync_response = not self._first_sync_done
        dispatch_persist_failure_rejected_response = False
        room_member_join_hooks_were_armed = self._room_member_join_hooks_armed
        room_member_join_hook_plan = _RoomMemberJoinSyncHookPlan()
        self._mark_sync_progress()

        if self._sync_shutting_down:
            return

        if isinstance(_response, nio.SyncResponse):
            with track_matrix_sync_cache_write(self.agent_name):
                try:
                    await self._apply_own_room_membership_from_sync(_response)
                except BaseException:
                    self._handle_pre_certification_failure()
                    raise
                restored_token_first_sync_response = (
                    first_sync_response and self._sync_cache_trust.state is SyncTrustState.PENDING
                )
                try:
                    cache_result = await self._conversation_cache.cache_sync_timeline_for_certification(_response)
                except asyncio.CancelledError as exc:
                    limited_room_ids, validation_errors = self._conversation_cache.limited_sync_timeline_room_ids(
                        _response,
                    )
                    cache_result = SyncCacheWriteResult(
                        complete=False,
                        limited_room_ids=limited_room_ids,
                        unrecovered_room_ids=tuple(sorted(_response.unrecovered_room_ids)),
                        errors=(*validation_errors, exc),
                    )
                    self._certify_sync_response(
                        next_batch=_response.next_batch,
                        cache_result=cache_result,
                        first_sync=first_sync_response,
                    )
                    raise
                decision = self._sync_cache_trust.plan_response(
                    next_batch=_response.next_batch,
                    cache_result=cache_result,
                    first_sync=first_sync_response,
                )
                room_member_join_hook_plan = self._room_member_join_sync_hook_plan(
                    first_sync_response=first_sync_response,
                    restored_token_first_sync_response=restored_token_first_sync_response,
                    hooks_were_armed=room_member_join_hooks_were_armed,
                    decision=decision,
                )
                try:
                    await self._run_pre_certification_sync_response_side_effects(
                        _response,
                        room_member_join_hook_plan=room_member_join_hook_plan,
                    )
                except BaseException:
                    self._handle_pre_certification_failure()
                    raise
                decision, room_member_join_hook_plan, dispatch_persist_failure_rejected_response = (
                    self._apply_sync_response_after_dispatch_acceptance(
                        decision,
                        cache_result=cache_result,
                        room_member_join_hook_plan=room_member_join_hook_plan,
                    )
                )
            self._mark_sync_progress()
        elif isinstance(_response, nio.SlidingSyncResponse):
            # Sliding sync never certifies the classic checkpoint, but the
            # account's own kicks and bans still must fence and purge rooms.
            with track_matrix_sync_cache_write(self.agent_name):
                await self._apply_own_room_membership_from_sliding_sync(_response)
            self._mark_sync_progress()
        if dispatch_persist_failure_rejected_response:
            return
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
            decision = self._sync_cache_trust.reject_unknown_pos()
            if decision.reset_client_token and self.client is not None:
                cast("Any", self.client).next_batch = None
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

    @staticmethod
    def _runtime_support_injection_error() -> str:
        """Return the shared error text for missing runtime support injection."""
        return (
            "Runtime support services must be injected before startup; "
            "AgentBot no longer supports standalone runtime support"
        )

    def _validate_runtime_support_injection_contract_for_startup(self) -> None:
        """Reject startup unless the full injected runtime-support bundle is present."""
        runtime = self._runtime_view
        if (
            runtime.event_cache is not None
            and runtime.event_cache_write_coordinator is not None
            and runtime.startup_thread_prewarm_registry is not None
        ):
            return
        raise PermanentMatrixStartupError(self._runtime_support_injection_error())

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
            ),
            nio.RoomMemberEvent,
        )
        if self._call_manager is None:
            return
        client.add_event_callback(
            _create_best_effort_task_wrapper(
                self._call_manager.on_room_event,
                owner=self._runtime_view,
            ),
            nio.UnknownEvent,
        )
        client.add_to_device_callback(
            _create_best_effort_task_wrapper(  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
                self._call_manager.on_to_device_event,
                owner=self._runtime_view,
            ),
            AuthenticatedToDeviceEvent,
        )

    async def _apply_own_room_membership_from_sync(self, response: nio.SyncResponse) -> None:
        """Apply this bot's authoritative joined/left room sections before other sync work."""
        joined_room_ids = set(response.rooms.join)
        left_room_ids = set(response.rooms.leave)
        timeline_departure_room_ids = {
            room_id
            for room_id, room_info in response.rooms.join.items()
            if any(
                isinstance(event, nio.RoomMemberEvent)
                and event.state_key == self.agent_user.user_id
                and event.membership in {"leave", "ban"}
                for event in room_info.timeline.events
            )
        }
        await self._apply_own_room_membership(
            joined_room_ids=joined_room_ids,
            left_room_ids=left_room_ids,
            departed_room_ids=left_room_ids | timeline_departure_room_ids,
        )

    async def _apply_own_room_membership_from_sliding_sync(self, response: nio.SlidingSyncResponse) -> None:
        """Apply this bot's room memberships reported by one sliding sync response."""
        joined_room_ids, departed_room_ids = sliding_own_membership_sets(response)
        await self._apply_own_room_membership(
            joined_room_ids=joined_room_ids,
            left_room_ids=departed_room_ids,
            departed_room_ids=departed_room_ids,
        )

    async def _apply_own_room_membership(
        self,
        *,
        joined_room_ids: set[str],
        left_room_ids: set[str],
        departed_room_ids: set[str],
    ) -> None:
        """Fence departed rooms and refresh joined-room cache access for one sync response."""
        if departed_room_ids:
            self._sync_cache_trust.invalidate_for_cache_scope_cleanup()
        for room_id in departed_room_ids:
            self._room_lifecycle.forget_invited_room(room_id)
        await self._conversation_cache.purge_rooms(departed_room_ids)
        self._local_departures_awaiting_sync.difference_update(departed_room_ids)
        current_joined_room_ids = joined_room_ids - left_room_ids - self._local_departures_awaiting_sync
        for room_id in current_joined_room_ids:
            await self._conversation_cache.mark_room_joined(room_id)
        call_manager = self._call_manager
        if call_manager is not None:
            await call_manager.on_sync_room_membership(
                joined_room_ids=current_joined_room_ids,
                left_room_ids=left_room_ids,
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
        self._validate_runtime_support_injection_contract_for_startup()
        await self.ensure_user_account()
        matrix_id_before_login = self.matrix_id
        self.client = await login_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
        )
        try:
            self._rebuild_runtime_components_after_login_if_identity_changed(matrix_id_before_login)
            self._turn_settlement_retry.bind_event_loop()
            orchestrator = self.orchestrator
            if orchestrator is not None:
                orchestrator.validate_managed_entity_identities()
            self._runtime_view.mark_runtime_started()
            await self._prepare_matrix_sync_continuity()
            await self._set_avatar_if_available()
            # Keep durable tracking-state loading off the event loop at startup.
            await asyncio.to_thread(self._turn_store.warm)
            await asyncio.to_thread(interactive.init_persistence, self.runtime_paths.storage_root)
            client = self.client
            assert client is not None

            # Persist correctness-critical source events; keep ID-less auxiliary inputs best-effort.
            client.add_event_callback(
                self._on_invite_before_sync_certification,  # ty: ignore[invalid-argument-type]
                nio.InviteEvent,  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
            )
            self._dispatch_obligation_runner.register_source_callbacks(
                client,
                owner=self._runtime_view,
            )
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
            create_background_task(
                self._recover_non_turn_dispatch_obligations(),
                name=f"recover_non_turn_dispatch_obligations_{self.agent_name}",
                owner=self._runtime_view,
            )
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

    async def _recover_non_turn_dispatch_obligations(self) -> None:
        """Retry non-turn callback discovery until the durable store is readable."""

        @retry(
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_not_exception_type(asyncio.CancelledError),
            before_sleep=before_sleep_log(self.logger, logging.WARNING),
            reraise=True,
        )
        async def recover() -> None:
            await self._dispatch_obligation_runner.recover_pending(turn_backed=False)

        await recover()

    async def recover_pending_turn_dispatch_obligations(self) -> None:
        """Release fleet-dependent turn replay after the responder startup pass."""
        await self._dispatch_obligation_runner.recover_pending(turn_backed=True)
        unsettled_source_event_ids = await asyncio.to_thread(
            self._dispatch_obligation_store.unsettled_source_event_ids,
        )
        await asyncio.to_thread(
            self._turn_store.cleanup,
            unsettled_source_event_ids=unsettled_source_event_ids,
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
                    on_room_left=self._purge_left_room,
                )
        except Exception:
            self.logger.exception("Error leaving rooms during cleanup")

        # Stop the bot
        await self.stop(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)

    async def _purge_left_room(self, room_id: str) -> None:
        """Fence and purge one principal-owned room immediately after departure."""
        self._local_departures_awaiting_sync.add(room_id)
        self._sync_cache_trust.invalidate_for_cache_scope_cleanup()
        await self._conversation_cache.purge_rooms((room_id,))

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

        if self.client is not None:
            self.logger.warning("Client is not None in stop()")
            await self.client.close()
        self.logger.info("Stopped agent bot")

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
                self.event_cache,
                self._conversation_cache,
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

    async def _cancel_startup_thread_prewarm(self) -> None:
        """Cancel the startup thread prewarm task if it is still running."""
        prewarm_task = self._startup_thread_prewarm_task
        self._startup_thread_prewarm_task = None
        if prewarm_task is None:
            return

        if not prewarm_task.done():
            prewarm_task.cancel()

        await asyncio.gather(prewarm_task, return_exceptions=True)

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
        self._response_runner.refuse_pending_admissions()
        await self._cancel_startup_thread_prewarm()
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
        if self._sync_cache_trust.state is SyncTrustState.CERTIFIED:
            self._sync_cache_trust.persist_current()
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
        await run_matrix_sync_forever(
            self.client,
            config=self.config,
            agent_name=self.agent_name,
            room_ids=self.rooms,
            timeout_ms=_SYNC_TIMEOUT_MS,
            sync_filter=_SYNC_FILTER,
            first_sync_done=self._first_sync_done,
        )

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        await self._room_lifecycle.on_invite(room, event)

    async def _on_invite_before_sync_certification(
        self,
        room: nio.MatrixRoom,
        event: nio.InviteEvent,
    ) -> None:
        """Durably accept invite work before scheduling its network side effects."""
        await self._dispatch_obligation_runner.dispatch_background(
            room,
            event,
            DispatchCallbackKind.INVITE,
            owner=self._runtime_view,
        )

    def _room_for_dispatch_obligation(self, room_id: str) -> nio.MatrixRoom:
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
            self._retry_pending_dispatch_source(
                pending_event.event.event_id,
                pending_event.callback_source_kind or pending_event.source_kind,
            )

    def _retry_pending_dispatch_source(self, source_event_id: str, source_kind: str) -> None:
        """Return one undelivered source to its exact durable callback owner."""
        self._dispatch_obligation_runner.retry_pending_turn_source(
            source_event_id,
            callback_kind_for_source_kind(source_kind),
        )

    async def _settle_ignored_dispatch_source(self, source_event_id: str, _source_kind: str) -> None:
        """Settle one asynchronously normalized source that produced no dispatch payload."""
        await self._dispatch_obligation_runner.settle_intentionally_ignored_turn_sources(
            (source_event_id,),
        )

    def _log_matrix_event_callback_started(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText | MatrixMediaEvent,
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

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> TurnDispatchOutcome:
        """Delegate one inbound text event to the turn engine."""
        receipt_time = time.monotonic()
        self._log_matrix_event_callback_started(room, event, callback_name="message")
        semantic_consumer = self._dispatch_obligation_runner.semantic_consumer()
        approval_reply_claimed = semantic_consumer is DispatchSemanticConsumer.APPROVAL_REPLY

        async def claim_approval_reply() -> None:
            nonlocal approval_reply_claimed
            await self._dispatch_obligation_runner.claim_semantic_consumer(
                DispatchSemanticConsumer.APPROVAL_REPLY,
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

    async def _on_redaction(self, room: nio.MatrixRoom, event: nio.Event) -> None:
        """Persist one redaction before updating advisory cache state."""
        assert isinstance(event, nio.RedactionEvent)
        await self._redacted_turn_cleanup.handle(room, event)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_thread_cache_scope():
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
    ) -> None:
        """Expose or record human joins that matrix-nio delivers through sync room state."""
        if self.agent_name != ROUTER_AGENT_NAME:
            return
        if not record_only and not self.hook_registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
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
        )
        for room, event in plan.dispatch_events:
            await self._dispatch_obligation_runner.dispatch(
                room,
                event,
                DispatchCallbackKind.ROOM_LIFECYCLE,
            )
        if plan.record_events:
            async with self._room_member_join_lock:
                await asyncio.to_thread(
                    record_room_member_joins_seen_from_events,
                    plan.record_events,
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
            await self._dispatch_obligation_runner.dispatch(
                room,
                event,
                DispatchCallbackKind.ROOM_LIFECYCLE,
            )

    async def _on_decryption_failure(self, room: nio.MatrixRoom, event: nio.MegolmEvent) -> None:
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
            conversation_cache=self._conversation_cache,
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
        *,
        runtime_started_at: float | None,
    ) -> AgentMessageSnapshot | None:
        """Read the latest visible cached sender message for hook helpers."""
        event_cache = self._runtime_view.event_cache
        if event_cache is None:
            self.logger.warning(
                "Agent-message snapshot requested before event cache is ready",
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
            )
            return None
        return await event_cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            sender,
            runtime_started_at=runtime_started_at,
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
        self._conversation_cache.notify_outbound_redaction(room_id, event_id)
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
