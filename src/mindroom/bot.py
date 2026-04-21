"""Matrix runtime shell for agents, teams, and the router."""

from __future__ import annotations

import asyncio
import time
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import nio
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.hooks import (
    AgentLifecycleContext,
    EnrichmentItem,
    HookContextSupport,
    HookRegistry,
    MessageEnvelope,
    ReactionReceivedContext,
    emit,
)
from mindroom.hooks.ingress import is_automation_source_kind
from mindroom.hooks.registry import HookRegistryState
from mindroom.hooks.sender import send_hook_message
from mindroom.hooks.types import EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED, EVENT_BOT_READY, EVENT_REACTION_RECEIVED
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.health import (
    clear_matrix_sync_state,
    mark_matrix_sync_loop_started,
    mark_matrix_sync_success,
)
from mindroom.matrix.identity import (
    MatrixID,
    extract_agent_name,
    is_agent_id,
)
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import leave_non_dm_rooms, resolve_room_aliases
from mindroom.matrix.state import MatrixState
from mindroom.matrix.sync_tokens import load_sync_token, save_sync_token
from mindroom.matrix.thread_membership import resolve_related_event_thread_id_best_effort
from mindroom.matrix.users import (
    AgentMatrixUser,
    create_agent_user,
    login_agent_user,
)
from mindroom.memory import store_conversation_memory
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.post_response_effects import (
    PostResponseEffectsSupport,
)
from mindroom.stop import StopManager
from mindroom.teams import TeamMode, TeamOutcome, resolve_configured_team
from mindroom.tool_approval import (
    TOOL_APPROVAL_RESPONSE_EVENT_TYPE,
    get_approval_store,
    handle_tool_approval_response_event,
)
from mindroom.tool_system.runtime_context import ToolRuntimeSupport
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
    tool_execution_identity,
)

from . import constants, interactive
from .agents import (
    create_agent,
    get_rooms_for_entity,
    show_tool_calls_for_agent,
)
from .authorization import (
    is_authorized_sender,
)
from .background_tasks import create_background_task, wait_for_background_tasks
from .coalescing import (
    CoalescedBatch,
    CoalescingGate,
)
from .commands import config_confirmation
from .commands.handler import _generate_welcome_message
from .constants import (
    ROUTER_AGENT_NAME,
    RuntimePaths,
    resolve_avatar_path,
)
from .conversation_resolver import (
    ConversationResolver,
    ConversationResolverDeps,
    MessageContext,
)
from .conversation_state_writer import (
    ConversationStateWriter,
    ConversationStateWriterDeps,
)
from .delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    EditTextRequest,
    ResponseHookService,
    SendTextRequest,
)
from .edit_regenerator import EditRegenerator, EditRegeneratorDeps
from .inbound_turn_normalizer import (
    DispatchPayload,
    InboundTurnNormalizer,
    InboundTurnNormalizerDeps,
)
from .knowledge.utils import KnowledgeAccessSupport, MultiKnowledgeVectorDb
from .logging_config import get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client import (
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    get_joined_rooms,
    join_room,
)
from .media_inputs import MediaInputs
from .response_runner import (
    ResponseRequest,
    ResponseRunner,
    ResponseRunnerDeps,
    prepare_memory_and_model_context,
)
from .runtime_support import (
    OwnedRuntimeSupport,
    close_owned_runtime_support,
    sync_owned_runtime_support,
)
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    has_deferred_overdue_tasks,
    restore_scheduled_tasks,
)
from .turn_controller import TurnController, TurnControllerDeps
from .turn_policy import (
    IngressHookRunner,
    TurnPolicy,
    TurnPolicyDeps,
)
from .turn_store import TurnStore, TurnStoreDeps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    import structlog
    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.matrix.cache.event_cache import ConversationEventCache
    from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = ["AgentBot", "MultiKnowledgeVectorDb"]


# Constants
_SYNC_TIMEOUT_MS = 30000
_STOPPING_RESPONSE_TEXT = "⏹️ Stopping generation..."


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
    _standalone_runtime_support: OwnedRuntimeSupport | None
    _turn_controller: TurnController

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
        self._hook_registry_state = HookRegistryState(HookRegistry.empty())
        self._standalone_runtime_support = None
        self._runtime_view = BotRuntimeState(
            client=None,
            config=config,
            enable_streaming=enable_streaming,
            orchestrator=None,
            event_cache=None,
            event_cache_write_coordinator=None,
        )
        self._deferred_overdue_task_drain_task = None
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
                storage_path=self.storage_path,
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
    def orchestrator(self) -> MultiAgentOrchestrator | None:
        """Return the current orchestrator."""
        return self._runtime_view.orchestrator

    @orchestrator.setter
    def orchestrator(self, value: MultiAgentOrchestrator | None) -> None:
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
                thread_id = await resolve_related_event_thread_id_best_effort(
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

    def _build_shared_execution_identity(self) -> ToolExecutionIdentity:
        """Build a non-request execution identity for shared agent materialization."""
        return build_tool_execution_identity(
            channel="matrix",
            agent_name=self.agent_name,
            runtime_paths=self.runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        )

    @property  # Not cached_property because Team mutates it!
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        if self.agent_name != ROUTER_AGENT_NAME and self.config.agents[self.agent_name].private is not None:
            msg = (
                f"AgentBot.agent is only available for shared agents. "
                f"Private agent '{self.agent_name}' requires an explicit execution identity."
            )
            raise ValueError(msg)
        execution_identity = self._build_shared_execution_identity()
        knowledge = self._knowledge_access_support.for_agent(self.agent_name)
        return create_agent(
            agent_name=self.agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            knowledge=knowledge,
            execution_identity=execution_identity,
            hook_registry=self.hook_registry,
        )

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        assert self.client is not None
        joined_rooms = await get_joined_rooms(self.client)
        current_rooms = set(joined_rooms or [])
        current_rooms.update(self.client.rooms)

        for room_id in self.rooms:
            if room_id in current_rooms:
                self.logger.debug("Already joined room", room_id=room_id)
                await self._post_join_room_setup(room_id)
                continue

            if await join_room(self.client, room_id):
                current_rooms.add(room_id)
                self.logger.info("Joined room", room_id=room_id)
                await self._post_join_room_setup(room_id)
            else:
                self.logger.warning("Failed to join room", room_id=room_id)

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
        assert self.client is not None

        # Get all rooms we're currently in
        joined_rooms = await get_joined_rooms(self.client)
        if joined_rooms is None:
            return

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.rooms)
        if self.agent_name == ROUTER_AGENT_NAME:
            # The router is the long-lived manager of the root Space even though it is
            # not part of the normal configured room list for conversational routing.
            root_space_id = MatrixState.load(runtime_paths=self.runtime_paths).space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        # Leave rooms we're no longer configured for (preserving DM rooms)
        await leave_non_dm_rooms(self.client, list(current_rooms - configured_rooms))

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

    def _restore_saved_sync_token(self) -> None:
        """Restore the saved Matrix sync token onto the current client."""
        assert self.client is not None
        try:
            token = load_sync_token(self.storage_path, self.agent_name)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            return

        if token is None:
            return

        self.client.next_batch = token
        self.logger.info("matrix_sync_token_restored")

    def _persist_sync_token(self) -> None:
        """Persist the current Matrix sync token."""
        if self.client is None:
            return
        token = self.client.next_batch
        if not isinstance(token, str) or not token:
            return

        try:
            save_sync_token(self.storage_path, self.agent_name, token)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_save_failed", error=str(exc))

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
            # Cache before persisting so a crash prefers replaying one batch over
            # skipping events whose timeline metadata never reached local state.
            self._conversation_cache.cache_sync_timeline(_response)

        # Event callbacks run fire-and-forget in background tasks. A crash after
        # persisting `next_batch` but before all callback tasks finish can still
        # lose events. That window is small, and tracking every background task
        # here would add more complexity than this restart optimization warrants.
        self._persist_sync_token()
        self._first_sync_done = True

        if first_sync_response:
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)

        if first_sync_response or has_deferred_overdue_tasks():
            self._maybe_start_deferred_overdue_task_drain()

    async def _on_sync_error(self, _response: nio.SyncError) -> None:
        """Update the watchdog clock on sync errors without marking cache state fresh."""
        logger.debug("SyncError received", agent_name=self.agent_name, error=str(_response))
        self._last_sync_monotonic = time.monotonic()

    async def ensure_rooms(self) -> None:
        """Ensure agent is in the correct rooms based on configuration.

        This consolidates room management into a single method that:
        1. Joins configured rooms
        2. Leaves unconfigured rooms
        """
        await self.join_configured_rooms()
        await self.leave_unconfigured_rooms()

    async def _initialize_runtime_support_services(self) -> None:
        """Initialize standalone runtime support services or accept a full injected pair."""
        binding_state = self._runtime_support_binding_state()
        if binding_state == "injected":
            self._runtime_view.mark_runtime_started()
            return
        if binding_state == "mixed":
            msg = self._partial_runtime_support_injection_error()
            raise RuntimeError(msg)
        self._runtime_view.mark_runtime_started()
        support = await sync_owned_runtime_support(
            self._standalone_runtime_support,
            db_path=self.config.cache.resolve_db_path(self.runtime_paths),
            logger=self.logger,
            background_task_owner=self._runtime_view,
            init_failure_reason_prefix="standalone_runtime_init_failed",
            log_db_path_change=False,
        )
        self._standalone_runtime_support = support
        self.event_cache = support.event_cache
        self.event_cache_write_coordinator = support.event_cache_write_coordinator

    async def _close_runtime_support_services(self) -> None:
        """Close standalone-owned services and detach any injected shared support."""
        binding_state = self._runtime_support_binding_state()
        if binding_state == "mixed":
            msg = self._partial_runtime_support_injection_error()
            raise RuntimeError(msg)
        if binding_state == "injected":
            return
        if binding_state == "uninitialized":
            return
        support = self._standalone_runtime_support
        assert support is not None
        await close_owned_runtime_support(support, logger=self.logger)
        self.event_cache = None
        self.event_cache_write_coordinator = None
        self._standalone_runtime_support = None

    def _runtime_support_binding_state(self) -> Literal["uninitialized", "standalone", "injected", "mixed"]:
        """Classify whether runtime support is local, injected, or inconsistently mixed."""
        runtime_event_cache = self._runtime_view.event_cache
        runtime_coordinator = self._runtime_view.event_cache_write_coordinator
        support = self._standalone_runtime_support
        if support is None:
            if runtime_event_cache is None and runtime_coordinator is None:
                return "uninitialized"
            if runtime_event_cache is not None and runtime_coordinator is not None:
                return "injected"
            return "mixed"
        uses_standalone_event_cache = runtime_event_cache is support.event_cache
        uses_standalone_coordinator = runtime_coordinator is support.event_cache_write_coordinator
        if uses_standalone_event_cache and uses_standalone_coordinator:
            return "standalone"
        if not uses_standalone_event_cache and not uses_standalone_coordinator:
            return "injected"
        return "mixed"

    @staticmethod
    def _partial_runtime_support_injection_error() -> str:
        """Return the shared error text for invalid mixed runtime support injection."""
        return "Runtime support services must be injected all together or not at all; partial injection is unsupported"

    def _validate_runtime_support_injection_contract_for_startup(self) -> None:
        """Reject mixed runtime support injection before startup side effects begin."""
        if self._runtime_support_binding_state() != "mixed":
            return
        raise PermanentMatrixStartupError(self._partial_runtime_support_injection_error())

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
            self._restore_saved_sync_token()
            await self._initialize_runtime_support_services()
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
            if self.agent_name == ROUTER_AGENT_NAME:
                client.add_event_callback(
                    _create_task_wrapper(self._on_unknown_event, owner=self._runtime_view),
                    nio.UnknownEvent,
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
            try:
                await self._close_runtime_support_services()
            except Exception:
                self.logger.warning("Failed to clean up runtime support after startup failure", exc_info=True)
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
            await self._close_runtime_support_services()
            await self.client.close()
        self.logger.info("Stopped agent bot")

    async def _send_welcome_message_if_empty(self, room_id: str) -> None:
        """Send a welcome message if the room has no messages yet.

        Only called by the router agent when joining a room.
        """
        assert self.client is not None

        # Check if room has any messages
        response = await self.client.room_messages(
            room_id,
            limit=2,  # Get 2 messages to check if we already sent welcome
            message_filter={"types": ["m.room.message"]},
        )

        # nio returns error types on failure - this is necessary
        if not isinstance(response, nio.RoomMessagesResponse):
            self.logger.error("Failed to check room messages", room_id=room_id, error=str(response))
            return

        # Only send welcome message if room is empty or only has our own welcome message
        if not response.chunk:
            # Room is completely empty
            self.logger.info("Room is empty, sending welcome message", room_id=room_id)

            # Generate and send the welcome message
            welcome_msg = _generate_welcome_message(room_id, self.config, self.runtime_paths)
            await self._send_response(
                room_id=room_id,
                reply_to_event_id=None,
                response_text=welcome_msg,
                thread_id=None,
                skip_mentions=True,
            )
            self.logger.info("Welcome message sent", room_id=room_id)
        elif len(response.chunk) == 1:
            # Check if the only message is our welcome message
            msg = response.chunk[0]
            if (
                isinstance(msg, nio.RoomMessageText)
                and msg.sender == self.agent_user.user_id
                and "Welcome to MindRoom" in msg.body
            ):
                self.logger.debug("Welcome message already sent", room_id=room_id)
                return
            # Otherwise, room has a different message, don't send welcome
        # Room has other messages, don't send welcome

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

    async def prepare_for_sync_shutdown(self) -> None:
        """Cancel work that must not outlive the Matrix sync loop."""
        self._sync_shutting_down = True
        await self._coalescing_gate.drain_all()
        self._persist_sync_token()
        if self.agent_name != ROUTER_AGENT_NAME:
            return

        await self._cancel_deferred_overdue_task_drain()

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        await self.client.sync_forever(timeout=_SYNC_TIMEOUT_MS, full_state=not self._first_sync_done)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        assert self.client is not None
        self.logger.info("Received invite", room_id=room.room_id, sender=event.sender)
        if await join_room(self.client, room.room_id):
            self.logger.info("Joined room", room_id=room.room_id)
            # If this is the router agent and the room is empty, send a welcome message
            if self.agent_name == ROUTER_AGENT_NAME:
                await self._send_welcome_message_if_empty(room.room_id)
        else:
            self.logger.error("Failed to join room", room_id=room.room_id)

    async def _dispatch_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Delegate one flushed coalesced batch to the turn engine."""
        await self._turn_controller.handle_coalesced_batch(batch)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Delegate one inbound text event to the turn engine."""
        await self._turn_controller.handle_text_event(room, event)

    async def _on_redaction(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Keep cached thread history consistent when Matrix redactions arrive."""
        await self._conversation_cache.apply_redaction(room.room_id, event)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._handle_reaction_inner(room, event)

    async def _on_unknown_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Handle custom Matrix event types that are outside nio's built-in event surface."""
        await self._handle_unknown_event_inner(room, event)

    async def _handle_reaction_inner(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle one reaction inside the per-turn thread-history cache scope."""
        assert self.client is not None

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
            if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
                self.logger.info(
                    "Stop requested for message",
                    message_id=event.reacts_to,
                    requested_by=event.sender,
                )
                await self.stop_manager.remove_stop_button(self.client, event.reacts_to)
                await self._send_response(room.room_id, event.reacts_to, _STOPPING_RESPONSE_TEXT, None)
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

    async def _handle_unknown_event_inner(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Resolve custom approval events on the router bot."""
        if self.agent_name != ROUTER_AGENT_NAME or event.type != TOOL_APPROVAL_RESPONSE_EVENT_TYPE:
            return
        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self.logger.debug("ignoring_unknown_event_from_unauthorized_sender", user_id=event.sender, type=event.type)
            return
        if not self._turn_policy.can_reply_to_sender(event.sender):
            self.logger.debug("Ignoring unknown event due to reply permissions", sender=event.sender, type=event.type)
            return

        store = get_approval_store()
        if store is None:
            self.logger.debug("ignoring_tool_approval_response_without_store", room_id=room.room_id)
            return

        resolved_request = await handle_tool_approval_response_event(
            store=store,
            room_id=room.room_id,
            sender_id=event.sender,
            event=event,
        )
        if resolved_request is None:
            return

        self.logger.info(
            "tool_approval_resolved_from_matrix_event",
            approval_request_id=resolved_request.id,
            tool_name=resolved_request.tool_name,
            status=resolved_request.status,
            resolved_by=resolved_request.resolved_by,
            room_id=room.room_id,
            thread_id=resolved_request.thread_id,
        )

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Delegate one inbound media event to the turn engine."""
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
        strip_transient_enrichment_after_run: bool = False,
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
                strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
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
        strip_transient_enrichment_after_run: bool = False,
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
            model_prompt: Optional model-facing prompt that may include transient enrichment.
            strip_transient_enrichment_after_run: Whether hook-provided transient enrichment
                must be scrubbed from persisted session history after this turn.
            system_enrichment_items: Hook-provided transient system prompt fragments to
                apply for this response before optional post-run scrubbing.
            response_envelope: Optional normalized inbound envelope for response hooks.
            correlation_id: Optional request correlation ID propagated to hook logging.
            target: Optional canonical response target used for lifecycle locking and delivery.
            matrix_run_metadata: Optional Matrix-specific run metadata persisted with the run
                for unseen-message tracking, coalesced edit regeneration, and cleanup.
            on_lifecycle_lock_acquired: Optional callback that runs after the response
                lifecycle lock is acquired and before response generation starts.

        Returns:
            Event ID of the response message, or None if failed

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
                strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
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
    ) -> str | None:
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
    ) -> str | None:
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
        strip_transient_enrichment_after_run: bool = False,
        system_enrichment_items: tuple[EnrichmentItem, ...] = (),
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        target: MessageTarget | None = None,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate a team response instead of individual agent response."""
        if not prompt.strip():
            return None

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
            if existing_event_id:
                await self._edit_message(room_id, existing_event_id, team_resolution.reason, thread_id)
                return existing_event_id
            return await self._send_response(
                room_id,
                reply_to_event_id,
                team_resolution.reason,
                thread_id,
            )
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
            strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
            system_enrichment_items=system_enrichment_items,
            correlation_id=correlation_id or reply_to_event_id,
            reason_prefix=f"Team '{self.agent_name}'",
            matrix_run_metadata=matrix_run_metadata,
            on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
        )
