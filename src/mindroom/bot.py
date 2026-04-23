"""Matrix runtime shell for agents, teams, and the router."""

from __future__ import annotations

import asyncio
import time
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import nio
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.dispatch_source import is_automation_source_kind
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    EVENT_REACTION_RECEIVED,
    AgentLifecycleContext,
    EnrichmentItem,
    HookContextSupport,
    HookRegistry,
    HookRegistryState,
    MessageEnvelope,
    ReactionReceivedContext,
    emit,
    send_hook_message,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.health import clear_matrix_sync_state, mark_matrix_sync_loop_started, mark_matrix_sync_success
from mindroom.matrix.identity import MatrixID, extract_agent_name, is_agent_id
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import leave_non_dm_rooms, resolve_room_aliases
from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCertificationDecision,
    SyncCheckpoint,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    start_from_loaded_token,
    sync_cache_write_diagnostics,
)
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_token_record, save_sync_token
from mindroom.matrix.users import AgentMatrixUser, create_agent_user, login_agent_user
from mindroom.memory import store_conversation_memory
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.post_response_effects import PostResponseEffectsSupport
from mindroom.stop import StopManager
from mindroom.teams import TeamMode, TeamOutcome, resolve_configured_team
from mindroom.tool_approval import AnchoredApprovalActionResult, get_approval_store
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.tool_system.worker_routing import tool_execution_identity

from . import constants, interactive
from .agents import create_agent, get_rooms_for_entity, show_tool_calls_for_agent
from .authorization import is_authorized_sender
from .background_tasks import create_background_task, wait_for_background_tasks
from .coalescing import CoalescingGate
from .commands import config_confirmation
from .constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_avatar_path
from .conversation_resolver import ConversationResolver, ConversationResolverDeps, MessageContext
from .conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from .delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    EditTextRequest,
    ResponseHookService,
    SendTextRequest,
)
from .edit_regenerator import EditRegenerator, EditRegeneratorDeps
from .inbound_turn_normalizer import DispatchPayload, InboundTurnNormalizer, InboundTurnNormalizerDeps
from .knowledge import KnowledgeAccessSupport
from .logging_config import get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client_room_admin import get_joined_rooms
from .matrix.client_session import PermanentMatrixStartupError
from .media_inputs import MediaInputs
from .response_runner import ResponseRequest, ResponseRunner, ResponseRunnerDeps, prepare_memory_and_model_context
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    has_deferred_overdue_tasks,
    restore_scheduled_tasks,
)
from .turn_controller import TurnController, TurnControllerDeps
from .turn_policy import IngressHookRunner, TurnPolicy, TurnPolicyDeps
from .turn_store import TurnStore, TurnStoreDeps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    import structlog
    from agno.agent import Agent

    from mindroom.coalescing_batch import CoalescedBatch
    from mindroom.config.main import Config
    from mindroom.matrix.cache import AgentMessageSnapshot, ConversationEventCache, EventCacheWriteCoordinator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.runtime_support import StartupThreadPrewarmRegistry
    from mindroom.tool_approval import ApprovalManager
    from mindroom.tool_system.events import ToolTraceEntry

type MatrixEventId = str

logger = get_logger(__name__)

__all__ = ["AgentBot"]


# Constants
_SYNC_TIMEOUT_MS = 30000
_STOPPING_RESPONSE_TEXT = "⏹️ Stopping generation..."


def _reply_to_event_id_from_event_source(event_source: dict[str, Any] | None) -> str | None:
    """Return the reply target event ID from one raw Matrix event source."""
    if not isinstance(event_source, dict):
        return None
    content = event_source.get("content")
    if not isinstance(content, dict):
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    in_reply_to = relates_to.get("m.in_reply_to")
    if not isinstance(in_reply_to, dict):
        return None
    reply_to_event_id = in_reply_to.get("event_id")
    return reply_to_event_id if isinstance(reply_to_event_id, str) else None


def _strip_matrix_rich_reply_fallback(body: str) -> str:
    """Remove the quoted fallback prefix from one Matrix rich-reply body."""
    lines = body.splitlines()
    quoted_line_count = 0
    while quoted_line_count < len(lines) and lines[quoted_line_count].startswith("> "):
        quoted_line_count += 1
    if quoted_line_count == 0 or quoted_line_count >= len(lines) or lines[quoted_line_count] != "":
        return body
    reply_body = "\n".join(lines[quoted_line_count + 1 :])
    return reply_body or body


def _create_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    *,
    owner: object | None = None,
) -> Callable[..., Awaitable[None]]:
    """Create a wrapper that runs the callback as a background task.

    This ensures the sync loop is never blocked by event processing,
    allowing the bot to handle new events (like stop reactions) while
    processing messages.
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
        # Convert team member agent names into canonical agent Matrix IDs.
        # Team streaming resolves config agents from these IDs, so they must keep
        # the `mindroom_` prefix used by MatrixID.from_agent().
        team_matrix_ids = [
            MatrixID.from_agent(agent_name, config.get_domain(runtime_paths), runtime_paths)
            for agent_name in team_config.agents
        ]
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths,
            rooms=rooms,
            config_path=config_path,
            team_agents=team_matrix_ids,
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


type _MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)

type _MessageContext = MessageContext


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
    _turn_store: TurnStore
    _tool_runtime_support: ToolRuntimeSupport
    _post_response_effects_support: PostResponseEffectsSupport
    _ingress_hook_runner: IngressHookRunner
    _hook_context_support: HookContextSupport
    _knowledge_access_support: KnowledgeAccessSupport
    _deferred_overdue_task_drain_task: asyncio.Task[None] | None
    _startup_thread_prewarm_task: asyncio.Task[None] | None
    _turn_controller: TurnController
    _room_lifecycle: BotRoomLifecycle
    _invited_rooms: set[str]

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
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._sync_shutting_down = False
        self._sync_trust_state = SyncTrustState.COLD
        self._sync_checkpoint: SyncCheckpoint | None = None
        self._hook_registry_state = HookRegistryState(HookRegistry.empty())
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
        self._deferred_overdue_task_drain_task = None
        self._startup_thread_prewarm_task = None
        self._room_lifecycle = BotRoomLifecycle(
            BotRoomLifecycleDeps(
                agent_name=self.agent_name,
                agent_user=self.agent_user,
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                get_logger=lambda: self.logger,
                get_configured_rooms=lambda: self.rooms,
                send_response=lambda *args, **kwargs: self._send_response(*args, **kwargs),
                on_configured_room_joined=lambda room_id: self._post_join_room_setup(room_id),
                on_router_invite_joined=lambda room_id: self._send_welcome_message_if_empty(room_id),
            ),
        )
        self._invited_rooms = self._room_lifecycle.invited_rooms
        self._init_runtime_components()

    def _init_runtime_components(self) -> None:
        """Initialize runtime-only helpers that depend on bound instance methods."""
        runtime_matrix_id = self.config.get_ids(self.runtime_paths).get(self.agent_name)
        if self.agent_user.user_id:
            runtime_matrix_id = self.matrix_id
        elif runtime_matrix_id is None:
            msg = f"Missing Matrix ID for {self.agent_name!r} during runtime initialization"
            raise KeyError(msg)
        self._coalescing_gate = CoalescingGate(
            dispatch_batch=self._dispatch_coalesced_batch,
            debounce_seconds=lambda: self.config.defaults.coalescing.debounce_ms / 1000,
            upload_grace_seconds=lambda: self.config.defaults.coalescing.upload_grace_ms / 1000,
            is_shutting_down=lambda: self._sync_shutting_down,
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
            logger=self.logger,
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
                sender_domain=runtime_matrix_id.domain,
                conversation_resolver=self._conversation_resolver,
            ),
        )
        self._delivery_gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                logger=self.logger,
                sender_domain=runtime_matrix_id.domain,
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
            ),
        )
        self._post_response_effects_support = PostResponseEffectsSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            delivery_gateway=self._delivery_gateway,
            conversation_cache=self._conversation_cache,
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
            ),
        )
        self._ingress_hook_runner = IngressHookRunner(
            hook_context=self._hook_context_support,
        )
        self._edit_regenerator = EditRegenerator(
            EditRegeneratorDeps(
                runtime=self._runtime_view,
                get_logger=lambda: self.logger,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                resolver=self._conversation_resolver,
                turn_store=self._turn_store,
                ingress_hook_runner=self._ingress_hook_runner,
                generate_response=lambda **kwargs: self._generate_response(**kwargs),
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
                turn_policy=self._turn_policy,
                ingress_hook_runner=self._ingress_hook_runner,
                response_runner=self._response_runner,
                delivery_gateway=self._delivery_gateway,
                tool_runtime=self._tool_runtime_support,
                turn_store=self._turn_store,
                coalescing_gate=self._coalescing_gate,
                edit_regenerator=self._edit_regenerator,
            ),
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

    @in_flight_response_count.setter
    def in_flight_response_count(self, value: int) -> None:
        """Update the number of active response lifecycles."""
        self._response_runner.in_flight_response_count = value

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
        try:
            completed = await self._conversation_cache.prewarm_recent_room_threads(
                room_id,
                is_shutting_down=lambda: self._sync_shutting_down,
            )
        except asyncio.CancelledError:
            await self.startup_thread_prewarm_registry.release(room_id)
            raise
        if not completed:
            await self.startup_thread_prewarm_registry.release(room_id)

    async def _run_startup_thread_prewarm(self) -> None:
        """Prewarm recent thread snapshots per joined room without blocking live dispatch behind cache seeding."""
        try:
            joined_rooms = await self._get_startup_thread_prewarm_joined_rooms()
            for room_id in joined_rooms:
                if self._sync_shutting_down:
                    return
                if not await self.startup_thread_prewarm_registry.try_claim(room_id):
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
                thread_id = await self._conversation_resolver.resolve_related_event_thread_id_best_effort(
                    room_id,
                    normalized_target_event_id,
                    access=self._conversation_resolver.thread_membership_access(
                        full_history=False,
                        dispatch_safe=True,
                    ),
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

    def _should_accept_invite(self) -> bool:
        """Return whether this entity should accept one inbound room invite."""
        return self._room_lifecycle.should_accept_invite()

    def _should_persist_invited_rooms(self) -> bool:
        """Return whether this entity persists invited room IDs across restarts."""
        return self._room_lifecycle.should_persist_invited_rooms()

    def _invited_rooms_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return self._room_lifecycle.invited_rooms_file_path()

    def _load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible entity."""
        return self._room_lifecycle.load_invited_rooms()

    def _save_invited_rooms(self) -> None:
        """Persist invited room IDs atomically for one eligible named agent."""
        self._room_lifecycle.save_invited_rooms()

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        await self._room_lifecycle.join_configured_rooms()

    async def _post_join_room_setup(self, room_id: str) -> None:
        """Run room setup that should happen after joins and across restarts."""
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

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for."""
        rooms_to_leave = await self._room_lifecycle.rooms_to_actually_leave()
        if rooms_to_leave and self.agent_name == ROUTER_AGENT_NAME and self.orchestrator is not None:
            await self.orchestrator._force_finalize_pending_approvals_for_rooms(set(rooms_to_leave))
        await self._room_lifecycle.leave_unconfigured_rooms(room_ids=rooms_to_leave)

    async def ensure_user_account(self) -> None:
        """Ensure this agent has a Matrix user account.

        This method makes the agent responsible for its own user account creation,
        moving this responsibility from the orchestrator to the agent itself.
        """
        # If we already have a user_id (e.g., provided by tests or config), assume account exists
        if self.agent_user.user_id:
            return
        # Create or retrieve the Matrix user account
        self.agent_user = await create_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_name,
            self.agent_user.display_name,  # Use existing display name if available
            runtime_paths=self.runtime_paths,
        )
        self.logger.info("ensured_matrix_user_account", user_id=self.agent_user.user_id)

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

        status_msg = build_agent_status_message(self.agent_name, self.config)
        await set_presence_status(self.client, status_msg)

    def mark_sync_loop_started(self) -> None:
        """Record that a sync loop iteration is starting.

        Does NOT arm the monotonic watchdog clock — that only starts when the
        first ``SyncResponse`` or ``SyncError`` arrives.  The watchdog has its
        own startup timeout for the pre-first-response window.
        """
        self._sync_shutting_down = False
        mark_matrix_sync_loop_started(self.agent_name)

    def reset_watchdog_clock(self) -> None:
        """Reset the monotonic watchdog clock for a fresh sync iteration."""
        self._last_sync_monotonic = None

    def _loaded_sync_token_for_certification(self) -> SyncCheckpoint | str | None:
        """Load a saved token record without deciding trust in bot code."""
        try:
            token_record = load_sync_token_record(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            return None
        if token_record is None:
            return None
        self.logger.info(
            "matrix_sync_token_restored",
            certified=token_record.certified,
        )
        return token_record.checkpoint if token_record.checkpoint is not None else token_record.token

    def _restore_saved_sync_token(self) -> None:
        """Restore Matrix sync continuity and initialize cache certification state."""
        assert self.client is not None
        startup = start_from_loaded_token(self._loaded_sync_token_for_certification())
        self._sync_trust_state = startup.state
        self._sync_checkpoint = None
        client = cast("Any", self.client)
        client.next_batch = startup.sync_token
        if startup.legacy_token:
            self.logger.warning("matrix_sync_token_uncertified_legacy")

    def _save_sync_checkpoint(self, checkpoint: SyncCheckpoint | None) -> None:
        """Persist one certified sync checkpoint if present."""
        if checkpoint is None:
            return
        try:
            save_sync_token(
                self.storage_path,
                self.agent_name,
                checkpoint.token,
            )
        except (OSError, ValueError) as exc:
            self.logger.warning("matrix_sync_token_save_failed", error=str(exc))

    def _clear_saved_sync_token(self) -> None:
        """Clear the saved sync token file."""
        try:
            clear_sync_token(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_clear_failed", error=str(exc))

    def _apply_sync_certification_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
    ) -> None:
        """Apply a certifier decision to runtime state and token storage."""
        self._sync_trust_state = decision.state
        self._sync_checkpoint = decision.checkpoint_to_save
        if decision.reset_client_token and self.client is not None:
            client = cast("Any", self.client)
            client.next_batch = None
        if decision.clear_saved_token:
            self._clear_saved_sync_token()
        if decision.checkpoint_to_save is not None:
            self._save_sync_checkpoint(decision.checkpoint_to_save)
        if decision.reason is not None:
            diagnostics = sync_cache_write_diagnostics(cache_result) if cache_result is not None else {}
            self.logger.warning(
                "matrix_sync_certification_uncertain",
                reason=decision.reason,
                **diagnostics,
            )

    async def _sync_cache_result_for_certification(self, response: nio.SyncResponse) -> SyncCacheWriteResult:
        """Return the durable cache write result for one sync response."""
        return await self._conversation_cache.cache_sync_timeline_for_certification(response)

    def _sync_certification_decision(
        self,
        response: nio.SyncResponse,
        *,
        cache_result: SyncCacheWriteResult,
        first_sync_response: bool,
    ) -> SyncCertificationDecision:
        """Return the certifier decision for one sync response."""
        return certify_sync_response(
            self._sync_trust_state,
            next_batch=response.next_batch,
            cache_result=cache_result,
            first_sync=first_sync_response,
        )

    def seconds_since_last_sync_activity(self) -> float | None:
        """Return elapsed seconds since the last sync-loop activity seen by the watchdog."""
        if self._last_sync_monotonic is None:
            return None
        return time.monotonic() - self._last_sync_monotonic

    async def _on_sync_response(self, _response: nio.SyncResponse) -> None:
        """Track successful sync responses for health checks and watchdogs."""
        first_sync_response = not self._first_sync_done
        self.last_sync_time = mark_matrix_sync_success(self.agent_name)
        self._last_sync_monotonic = time.monotonic()

        if self._sync_shutting_down:
            return

        if isinstance(_response, nio.SyncResponse):
            try:
                cache_result = await self._sync_cache_result_for_certification(_response)
            except asyncio.CancelledError as exc:
                cache_result = SyncCacheWriteResult(complete=False, errors=(exc,))
                decision = self._sync_certification_decision(
                    _response,
                    cache_result=cache_result,
                    first_sync_response=first_sync_response,
                )
                self._apply_sync_certification_decision(decision, cache_result=cache_result)
                raise
            decision = self._sync_certification_decision(
                _response,
                cache_result=cache_result,
                first_sync_response=first_sync_response,
            )
            self._apply_sync_certification_decision(decision, cache_result=cache_result)
        self._first_sync_done = True

        if first_sync_response:
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)
            orchestrator = self.orchestrator
            if orchestrator is not None:
                await orchestrator._handle_bot_ready(self)
            self._maybe_start_startup_thread_prewarm()

        if first_sync_response or has_deferred_overdue_tasks():
            self._maybe_start_deferred_overdue_task_drain()

    async def _on_sync_error(self, _response: nio.SyncError) -> None:
        """Update the watchdog clock on sync errors without marking cache state fresh."""
        logger.debug("SyncError received", agent_name=self.agent_name, error=str(_response))
        self._last_sync_monotonic = time.monotonic()
        if _response.status_code == "M_UNKNOWN_POS":
            self._apply_sync_certification_decision(handle_unknown_pos())
            self.logger.warning(
                "matrix_sync_token_rejected",
                status_code=_response.status_code,
                error=str(_response),
                first_sync=not self._first_sync_done,
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

    async def start(self) -> None:
        """Start the agent bot with user account setup (but don't join rooms yet)."""
        self._validate_runtime_support_injection_contract_for_startup()
        await self.ensure_user_account()
        self.client = await login_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
        )
        try:
            self._runtime_view.mark_runtime_started()
            self._restore_saved_sync_token()
            await self._set_avatar_if_available()
            await self._set_presence_with_model_info()
            interactive.init_persistence(self.runtime_paths.storage_root)
            client = self.client
            assert client is not None

            # Register event callbacks - wrap them to run as background tasks
            # This ensures the sync loop is never blocked, allowing stop reactions to work
            client.add_event_callback(
                _create_task_wrapper(self._on_invite, owner=self._runtime_view),
                nio.InviteEvent,  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
            )
            client.add_event_callback(
                _create_task_wrapper(self._on_message, owner=self._runtime_view),
                nio.RoomMessageText,
            )
            client.add_event_callback(
                _create_task_wrapper(self._on_redaction, owner=self._runtime_view),
                nio.RedactionEvent,
            )
            client.add_event_callback(
                _create_task_wrapper(self._on_reaction, owner=self._runtime_view),
                nio.ReactionEvent,
            )

            # Register media callbacks on all agents (each agent handles its own routing)
            media_callback = _create_task_wrapper(self._on_media_message, owner=self._runtime_view)
            for event_type in (
                nio.RoomMessageImage,
                nio.RoomEncryptedImage,
                nio.RoomMessageFile,
                nio.RoomEncryptedFile,
                nio.RoomMessageVideo,
                nio.RoomEncryptedVideo,
                nio.RoomMessageAudio,
                nio.RoomEncryptedAudio,
            ):
                client.add_event_callback(media_callback, event_type)
            client.add_event_callback(
                _create_task_wrapper(self._on_unknown_event, owner=self._runtime_view),
                nio.UnknownEvent,
            )
            client.add_response_callback(self._on_sync_response, nio.SyncResponse)  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
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

    async def try_start(self) -> bool:
        """Try to start the agent bot with smart retry logic.

        Retries transient failures but stops immediately on permanent startup errors.

        Returns:
            True if the bot started successfully, False otherwise.

        """

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_not_exception_type(PermanentMatrixStartupError),
            reraise=True,
        )
        async def _start_with_retry() -> None:
            await self.start()

        try:
            await _start_with_retry()
            return True  # noqa: TRY300
        except Exception as exc:
            if isinstance(exc, PermanentMatrixStartupError):
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
                await leave_non_dm_rooms(self.client, joined_rooms)
        except Exception:
            self.logger.exception("Error leaving rooms during cleanup")

        # Stop the bot
        await self.stop(reason="entity_removed")

    async def stop(self, *, reason: str | None = None) -> None:
        """Stop the agent bot."""
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        clear_matrix_sync_state(self.agent_name)
        await self._emit_agent_lifecycle_event(EVENT_AGENT_STOPPED, stop_reason=reason)

        await self.prepare_for_sync_shutdown()

        # Wait for any pending background tasks (like memory saves) to complete
        try:
            await wait_for_background_tasks(timeout=5.0, owner=self._runtime_view)  # 5 second timeout
            self.logger.info("Background tasks completed")
        except Exception as e:
            self.logger.warning("background_tasks_incomplete", error=str(e))

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

    async def _send_welcome_message_if_empty(self, room_id: str) -> None:
        """Send a welcome message if the room has no messages yet.

        Only called by the router agent when joining a room.
        """
        await self._room_lifecycle.send_welcome_message_if_empty(room_id)

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

    async def prepare_for_sync_shutdown(self) -> None:
        """Cancel work that must not outlive the Matrix sync loop."""
        self._sync_shutting_down = True
        await self._cancel_startup_thread_prewarm()
        await self._coalescing_gate.drain_all()
        if self._sync_trust_state is SyncTrustState.CERTIFIED:
            self._save_sync_checkpoint(self._sync_checkpoint)
        if self.agent_name != ROUTER_AGENT_NAME:
            return

        await self._cancel_deferred_overdue_task_drain()

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        await self.client.sync_forever(timeout=_SYNC_TIMEOUT_MS, full_state=not self._first_sync_done)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        await self._room_lifecycle.on_invite(room, event)

    async def _dispatch_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Delegate one flushed coalesced batch to the turn engine."""
        await self._turn_controller.handle_coalesced_batch(batch)

    @staticmethod
    def _origin_server_ts_from_source(source: object) -> int | float | None:
        """Return a Matrix origin timestamp from a raw event source if present."""
        if not isinstance(source, dict):
            return None
        source_dict = cast("dict[str, object]", source)
        raw_timestamp = source_dict.get("origin_server_ts")
        if isinstance(raw_timestamp, int | float) and not isinstance(raw_timestamp, bool):
            return raw_timestamp
        return None

    def _log_matrix_event_callback_started(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText | _MediaDispatchEvent,
        *,
        callback_name: str,
    ) -> None:
        """Log Matrix ingress timing without message content."""
        receive_timestamp_ms = int(time.time() * 1000)
        origin_server_ts = self._origin_server_ts_from_source(event.source)
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

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Delegate one inbound text event to the turn engine."""
        self._log_matrix_event_callback_started(room, event, callback_name="message")
        if await self._maybe_handle_tool_approval_reply(room, event):
            return
        await self._turn_controller.handle_text_event(room, event)

    async def _on_redaction(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Keep cached thread history consistent when Matrix redactions arrive."""
        await self._conversation_cache.apply_redaction(room.room_id, event)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._handle_reaction_inner(room, event)

    async def _on_unknown_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Handle custom Matrix events that are not part of nio's typed event set."""
        if event.type != "io.mindroom.tool_approval_response":
            return
        approval_event_id, status, reason = self._approval_response_payload(event)
        if approval_event_id is None or status is None:
            return
        approval_manager = get_approval_store()
        if approval_manager is None:
            return
        pending = approval_manager.anchored_request_for_event(
            approval_event_id=approval_event_id,
            room_id=room.room_id,
        )
        if pending is None:
            return
        if pending.status != "pending" and pending.resolution_synced_at is None:
            await approval_manager.replay_resolved_card_for_room(
                approval_event_id=approval_event_id,
                room_id=room.room_id,
            )
            return
        sender_is_requester = pending.approver_user_id == event.sender
        pending_is_open = pending.status == "pending"

        if not self._sender_can_resolve_tool_approval(room, event.sender):
            if pending_is_open and sender_is_requester:
                await approval_manager.deny_anchored_request_for_lost_authorization(
                    approval_event_id=approval_event_id,
                    room_id=room.room_id,
                    resolved_by=event.sender,
                )
            elif pending_is_open:
                await approval_manager.restate_pending_anchored_request(
                    approval_event_id=approval_event_id,
                    room_id=room.room_id,
                )
            return

        result = await approval_manager.handle_custom_response(
            approval_event_id=approval_event_id,
            room_id=room.room_id,
            status=status,
            reason=reason,
            resolved_by=event.sender,
        )
        if not result.handled and pending_is_open and not sender_is_requester:
            await approval_manager.restate_pending_anchored_request(
                approval_event_id=approval_event_id,
                room_id=room.room_id,
            )

    @staticmethod
    def _approval_response_payload(
        event: nio.UnknownEvent,
    ) -> tuple[str | None, Literal["approved", "denied"] | None, str | None]:
        """Parse one custom approval response event anchored to the approval card."""
        content = event.source.get("content", {})
        if not isinstance(content, dict):
            return None, None, None

        approval_event_id = _reply_to_event_id_from_event_source(event.source)
        if approval_event_id is None:
            return None, None, None

        raw_status = content.get("status")
        if raw_status not in {"approved", "denied"}:
            return approval_event_id, None, None

        raw_reason = content.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else None
        return approval_event_id, raw_status, reason

    async def _maybe_handle_tool_approval_reply(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
    ) -> bool:
        """Consume reply-to-approval messages as denial actions."""
        approval_manager = get_approval_store()
        if approval_manager is None:
            return False

        reply_to_event_id = _reply_to_event_id_from_event_source(event.source)
        if reply_to_event_id is None:
            return False
        pending = approval_manager.anchored_request_for_event(
            approval_event_id=reply_to_event_id,
            room_id=room.room_id,
        )
        if pending is None:
            return False

        await self._handle_tool_approval_action(
            room=room,
            sender_id=event.sender,
            approval_event_id=reply_to_event_id,
            action=lambda resolved_approval_manager: resolved_approval_manager.handle_reply(
                approval_event_id=reply_to_event_id,
                room_id=room.room_id,
                reason=_strip_matrix_rich_reply_fallback(event.body),
                resolved_by=event.sender,
            ),
        )
        return True

    async def _handle_reaction_inner(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle one reaction inside the per-turn thread-history cache scope."""
        assert self.client is not None

        if await self._handle_tool_approval_action(
            room=room,
            sender_id=event.sender,
            approval_event_id=event.reacts_to,
            action=lambda approval_manager: approval_manager.handle_reaction(
                approval_event_id=event.reacts_to,
                room_id=room.room_id,
                reaction_key=event.key,
                resolved_by=event.sender,
            ),
        ):
            return

        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self.logger.debug("ignoring_reaction_from_unauthorized_sender", user_id=event.sender)
            return

        if not self._turn_policy.can_reply_to_sender(event.sender):
            self.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
            return

        if event.key == "🛑":
            sender_agent_name = extract_agent_name(event.sender, self.config, self.runtime_paths)
            tracked_target = self.stop_manager.get_tracked_target(event.reacts_to)
            if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
                self.logger.info(
                    "Stop requested for message",
                    message_id=event.reacts_to,
                    requested_by=event.sender,
                )
                await self.stop_manager.remove_stop_button(
                    self.client,
                    event.reacts_to,
                    notify_outbound_redaction=self._conversation_cache.notify_outbound_redaction,
                )
                await self._send_response(
                    room.room_id,
                    event.reacts_to,
                    _STOPPING_RESPONSE_TEXT,
                    tracked_target.resolved_thread_id if tracked_target is not None else None,
                )
                return

        pending_change = config_confirmation.get_pending_change(event.reacts_to)
        if pending_change and self.agent_name == ROUTER_AGENT_NAME:
            await config_confirmation.handle_confirmation_reaction(self, room, event, pending_change)
            return

        result = await interactive.handle_reaction(
            self.client,
            event,
            self.agent_name,
            self.config,
            self.runtime_paths,
        )
        if result:
            await self._turn_controller.handle_interactive_selection(
                room,
                selection=result,
                user_id=event.sender,
            )
            return

        await self._emit_reaction_received_hooks(
            room_id=room.room_id,
            event=event,
            correlation_id=event.event_id,
        )

    def _sender_can_resolve_tool_approval(self, room: nio.MatrixRoom, sender_id: str) -> bool:
        """Return whether the sender is currently allowed to act on a tool approval."""
        if not is_authorized_sender(
            sender_id,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self.logger.debug("ignoring_tool_approval_action_from_unauthorized_sender", user_id=sender_id)
            return False

        return True

    async def _handle_tool_approval_action(
        self,
        *,
        room: nio.MatrixRoom,
        sender_id: str,
        approval_event_id: str,
        action: Callable[[ApprovalManager], Awaitable[AnchoredApprovalActionResult]],
    ) -> bool:
        """Resolve one approval action only when the sender still has access."""
        approval_manager = get_approval_store()
        if approval_manager is None:
            return False
        if not self._sender_can_resolve_tool_approval(room, sender_id):
            return False
        result = await action(approval_manager)
        orchestrator = self.orchestrator
        if (
            result.error_reason is not None
            and orchestrator is not None
            and self._should_send_tool_approval_notice(room_id=room.room_id)
        ):
            await orchestrator._send_approval_notice(
                room_id=room.room_id,
                approval_event_id=approval_event_id,
                thread_id=result.thread_id,
                reason=result.error_reason,
            )
        return result.handled

    def _should_send_tool_approval_notice(
        self,
        *,
        room_id: str,
    ) -> bool:
        """Return whether this bot owns router-scoped approval notice transport."""
        orchestrator = self.orchestrator
        if orchestrator is None or self.client is None:
            return False
        transport_bot = orchestrator._approval_transport_bot(room_id)
        return transport_bot is not None and transport_bot.client is self.client

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Delegate one inbound media event to the turn engine."""
        self._log_matrix_event_callback_started(room, event, callback_name="media")
        await self._turn_controller.handle_media_event(room, event)

    def _should_queue_follow_up_in_active_response_thread(
        self,
        *,
        context: _MessageContext,
        target: MessageTarget | None,
        source_envelope: MessageEnvelope | None,
    ) -> bool:
        """Return whether one human follow-up should enter the queued-response path."""
        if target is None or source_envelope is None or not context.is_thread:
            return False
        if context.mentioned_agents or context.has_non_agent_mentions:
            return False
        if is_automation_source_kind(source_envelope.source_kind):
            return False
        if is_agent_id(source_envelope.sender_id, self.config, self.runtime_paths):
            return False
        return self.has_active_response_for_target(target)

    def _agent_has_matrix_messaging_tool(self, agent_name: str) -> bool:
        """Return whether an agent can issue Matrix message actions."""
        try:
            tool_names = self.config.get_agent_tools(agent_name)
        except ValueError:
            return False
        if not isinstance(tool_names, list | tuple | set):
            return False
        return "matrix_message" in tool_names

    async def _generate_team_response_helper(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        team_agents: list[MatrixID],
        team_mode: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        requester_user_id: str,
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        *,
        target: MessageTarget | None = None,
        payload: DispatchPayload,
        response_envelope: MessageEnvelope | None = None,
        system_enrichment_items: tuple[EnrichmentItem, ...] = (),
        correlation_id: str | None = None,
        reason_prefix: str = "Team request",
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate a team response (shared between preformed teams and TeamBot)."""
        return await self._response_runner.generate_team_response_helper(
            ResponseRequest(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=payload.prompt,
                model_prompt=payload.model_prompt,
                existing_event_id=existing_event_id,
                existing_event_is_placeholder=existing_event_is_placeholder,
                user_id=requester_user_id,
                media=payload.media,
                attachment_ids=tuple(payload.attachment_ids) if payload.attachment_ids is not None else None,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
                target=target,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
                on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
            ),
            team_agents=team_agents,
            team_mode=team_mode,
            reason_prefix=reason_prefix,
        )

    async def _generate_response(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
        model_prompt: str | None = None,
        system_enrichment_items: tuple[EnrichmentItem, ...] = (),
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        target: MessageTarget | None = None,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate and send/edit a response using AI.

        Args:
            room_id: The room to send the response to
            prompt: The prompt to send to the AI
            reply_to_event_id: The event to reply to
            thread_id: Thread ID if in a thread
            thread_history: Thread history for context
            existing_event_id: If provided, edit this message instead of sending a new one
                             (used for placeholders and interactive acknowledgments)
            existing_event_is_placeholder: Whether `existing_event_id` points at a
                             provisional visible event that may be cleaned up on suppression
            user_id: User ID of the sender for identifying user messages in history
            media: Optional multimodal inputs (audio/images/files/videos)
            attachment_ids: Attachment IDs available for tool-side file processing
            model_prompt: Optional model-facing prompt for the live request and persisted history.
            system_enrichment_items: Hook-provided transient system prompt fragments to
                apply for this response.
            response_envelope: Optional normalized inbound envelope for response hooks.
            correlation_id: Optional request correlation ID propagated to hook logging.
            target: Optional canonical response target used for lifecycle locking and delivery.
            matrix_run_metadata: Optional Matrix-specific run metadata persisted with the run
                for unseen-message tracking, coalesced edit regeneration, and cleanup.
            on_lifecycle_lock_acquired: Optional callback that runs after the response
                lifecycle lock is acquired and before response generation starts.

        Returns:
            Event ID of the visible response, or None if no visible response landed.

        """
        return await self._response_runner.generate_response(
            ResponseRequest(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=prompt,
                model_prompt=model_prompt,
                existing_event_id=existing_event_id,
                existing_event_is_placeholder=existing_event_is_placeholder,
                user_id=user_id,
                media=media,
                attachment_ids=tuple(attachment_ids) if attachment_ids is not None else None,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
                target=target,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
                on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
            ),
        )

    async def _send_response(
        self,
        room_id: str,
        reply_to_event_id: str | None,
        response_text: str,
        thread_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
        skip_mentions: bool = False,
        tool_trace: list[ToolTraceEntry] | None = None,
        extra_content: dict[str, Any] | None = None,
        thread_mode_override: Literal["thread", "room"] | None = None,
        target: MessageTarget | None = None,
    ) -> MatrixEventId | None:
        """Send a response message to a room."""
        resolved_target = target or self._conversation_resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            event_source=reply_to_event.source if reply_to_event is not None else None,
            thread_mode_override=thread_mode_override,
        )
        return await self._delivery_gateway.send_text(
            SendTextRequest(
                target=resolved_target,
                response_text=response_text,
                skip_mentions=skip_mentions,
                tool_trace=tool_trace,
                extra_content=extra_content,
            ),
        )

    async def _hook_send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None = None,
        *,
        trigger_dispatch: bool = False,
    ) -> MatrixEventId | None:
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
            sender_domain=self.matrix_id.domain,
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

    async def _edit_message(
        self,
        room_id: str,
        event_id: str,
        new_text: str,
        thread_id: str | None,
        tool_trace: list[ToolTraceEntry] | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> bool:
        """Edit an existing message.

        Returns:
            True if edit was successful, False otherwise.

        """
        return await self._delivery_gateway.edit_text(
            EditTextRequest(
                target=self._conversation_resolver.build_message_target(
                    room_id=room_id,
                    thread_id=thread_id,
                    reply_to_event_id=None,
                ),
                event_id=event_id,
                new_text=new_text,
                tool_trace=tool_trace,
                extra_content=extra_content,
            ),
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
    team_agents: list[MatrixID]
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
        team_agents: list[MatrixID] | None = None,
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
        self.team_agents = [] if team_agents is None else team_agents
        self.team_mode = team_mode
        self.team_model = team_model

    @cached_property
    def agent(self) -> Agent | None:
        """Teams don't have individual agents, return None."""
        return None

    async def _generate_response(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
        model_prompt: str | None = None,
        system_enrichment_items: tuple[EnrichmentItem, ...] = (),
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        target: MessageTarget | None = None,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate a team response instead of individual agent response."""
        if not prompt.strip():
            return await self._response_runner.generate_response_for_empty_prompt(
                ResponseRequest(
                    room_id=room_id,
                    reply_to_event_id=reply_to_event_id,
                    thread_id=thread_id,
                    thread_history=thread_history,
                    prompt=prompt,
                    model_prompt=model_prompt,
                    existing_event_id=existing_event_id,
                    existing_event_is_placeholder=existing_event_is_placeholder,
                    user_id=user_id,
                    media=media,
                    attachment_ids=tuple(attachment_ids) if attachment_ids is not None else None,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                    target=target,
                    matrix_run_metadata=matrix_run_metadata,
                    system_enrichment_items=system_enrichment_items,
                    on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
                ),
                response_kind="team",
            )
        assert self.client is not None
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            prepare_memory_and_model_context(
                prompt,
                thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                model_prompt=model_prompt,
            )
        )

        configured_mode = TeamMode.COORDINATE if self.team_mode == "coordinate" else TeamMode.COLLABORATE
        materializable_agent_names = self._turn_policy.materializable_agent_names()
        team_resolution = resolve_configured_team(
            self.agent_name,
            self.team_agents,
            configured_mode,
            self.config,
            self.runtime_paths,
            materializable_agent_names=materializable_agent_names,
        )
        if team_resolution.outcome is not TeamOutcome.TEAM:
            assert team_resolution.reason is not None
            response_event_id: str | None
            if existing_event_id:
                edited = await self._edit_message(
                    room_id=room_id,
                    event_id=existing_event_id,
                    new_text=team_resolution.reason,
                    thread_id=thread_id,
                )
                response_event_id = existing_event_id if edited else None
            else:
                response_event_id = await self._send_response(
                    room_id=room_id,
                    reply_to_event_id=reply_to_event_id,
                    response_text=team_resolution.reason,
                    thread_id=thread_id,
                )
            return response_event_id
        assert team_resolution.mode is not None

        resolved_target = target or self._conversation_resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )
        agent_names = [
            mid.agent_name(self.config, self.runtime_paths) or mid.username for mid in team_resolution.eligible_members
        ]
        session_id = resolved_target.session_id
        execution_identity = self._tool_runtime_support.build_execution_identity(
            target=resolved_target,
            user_id=user_id,
            session_id=session_id,
        )
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
                    user_id,
                    execution_identity=execution_identity,
                ),
                name=f"memory_save_team_{session_id}",
                owner=self._runtime_view,
            )

        media_inputs = media or MediaInputs()

        return await self._generate_team_response_helper(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            target=resolved_target,
            payload=DispatchPayload(
                prompt=memory_prompt,
                model_prompt=model_prompt_text,
                media=media_inputs,
                attachment_ids=attachment_ids,
            ),
            team_agents=team_resolution.eligible_members,
            team_mode=team_resolution.mode.value,
            thread_history=model_thread_history,
            requester_user_id=user_id or "",
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            response_envelope=response_envelope
            or MessageEnvelope(
                source_event_id=reply_to_event_id,
                room_id=room_id,
                target=resolved_target,
                requester_id=user_id or self.matrix_id.full_id,
                sender_id=user_id or self.matrix_id.full_id,
                body=memory_prompt,
                attachment_ids=tuple(attachment_ids or ()),
                mentioned_agents=(),
                agent_name=self.agent_name,
                source_kind="message",
            ),
            system_enrichment_items=system_enrichment_items,
            correlation_id=correlation_id or reply_to_event_id,
            reason_prefix=f"Team '{self.agent_name}'",
            matrix_run_metadata=matrix_run_metadata,
            on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
        )
