"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal, cast
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
from mindroom.hooks.ingress import (
    hook_ingress_policy,
    is_automation_source_kind,
    should_handle_interactive_text_response,
)
from mindroom.hooks.registry import HookRegistryState
from mindroom.hooks.sender import send_hook_message
from mindroom.hooks.types import EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED, EVENT_BOT_READY, EVENT_REACTION_RECEIVED
from mindroom.matrix.event_info import EventInfo
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
from mindroom.matrix.message_content import (
    extract_edit_body,
    is_v2_sidecar_text_preview,
)
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.room_cache import cached_room_get_event
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import leave_non_dm_rooms, resolve_room_aliases
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import (
    AgentMatrixUser,
    create_agent_user,
    login_agent_user,
)
from mindroom.memory import store_conversation_memory
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.post_response_effects import (
    PostResponseEffectsSupport,
    matrix_run_metadata_for_handled_turn,
    record_handled_turn,
)
from mindroom.stop import StopManager
from mindroom.teams import TeamMode, TeamOutcome, TeamResolution, resolve_configured_team
from mindroom.thread_utils import (
    should_agent_respond,
)
from mindroom.timing import (
    attach_dispatch_pipeline_timing,
    create_dispatch_pipeline_timing,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import timing_scope as timing_scope_context
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
    remove_run_by_event_id,
    show_tool_calls_for_agent,
)
from .attachments import (
    merge_attachment_ids,
    parse_attachment_ids_from_event_source,
)
from .authorization import (
    get_effective_sender_id_for_reply_permissions,
    is_authorized_sender,
)
from .background_tasks import create_background_task, wait_for_background_tasks
from .coalescing import (
    CoalescedBatch,
    CoalescingGate,
    CoalescingKey,
    PendingEvent,
    PreparedTextEvent,
    build_batch_dispatch_event,
    coalesced_prompt,
)
from .commands import config_confirmation
from .commands.handler import CommandEvent, _generate_welcome_message
from .commands.parsing import Command, command_parser
from .constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
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
    LoadPersistedTurnMetadataRequest,
    PersistedTurnMetadata,
    RemoveStaleRunsRequest,
)
from .delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryResult,
    EditTextRequest,
    ResponseHookService,
    SendTextRequest,
)
from .delivery_gateway import (
    SuppressedPlaceholderCleanupError as _SuppressedPlaceholderCleanupError,
)
from .dispatch_planner import (
    DispatchHookService,
    DispatchPlan,
    DispatchPlanner,
    DispatchPlannerDeps,
    ResponseAction,
)
from .dispatch_planner import (
    PreparedDispatch as _PreparedDispatch,
)
from .dispatch_planner import (
    ResponseAction as _ResponseAction,
)
from .handled_turns import HandledTurnLedger, HandledTurnRecord, HandledTurnState
from .inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    DispatchPayload,
    DispatchPayloadWithAttachmentsRequest,
    InboundTurnNormalizer,
    InboundTurnNormalizerDeps,
    TextNormalizationRequest,
    VoiceNormalizationRequest,
)
from .knowledge.utils import (
    KnowledgeAccessSupport,
    MultiKnowledgeVectorDb,
)
from .logging_config import emoji, get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client import (
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    get_joined_rooms,
    join_room,
)
from .matrix.event_cache import EventCache, normalize_event_source_for_cache
from .media_inputs import MediaInputs
from .response_coordinator import (
    ResponseCoordinator,
    ResponseCoordinatorDeps,
    ResponseRequest,
    prepare_memory_and_model_context,
)
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    has_deferred_overdue_tasks,
    restore_scheduled_tasks,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    import structlog
    from agno.agent import Agent
    from agno.media import Image

    from mindroom.config.main import Config
    from mindroom.history import CompactionOutcome
    from mindroom.history.types import HistoryScope
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = ["AgentBot", "MultiKnowledgeVectorDb"]


# Constants
_SYNC_TIMEOUT_MS = 30000
_STOPPING_RESPONSE_TEXT = "⏹️ Stopping generation..."
_RECEIVED_MONOTONIC_KEY = "com.mindroom.received_monotonic"


def _thread_summary_message_count_hint(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> int:
    """Return a lower-bound post-response thread size without refetching history.

    The summary task runs only after this bot has already appended one visible
    reply to the thread, so the hint must account for that new non-summary
    message. Existing summary notices do not count toward the thresholds.
    """
    existing_non_summary_messages = sum(
        1 for message in thread_history if not isinstance(message.content.get("io.mindroom.thread_summary"), dict)
    )
    return existing_non_summary_messages + 1


def _create_task_wrapper(
    callback: Callable[..., Awaitable[None]],
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
        create_background_task(error_handler())

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
type _InboundMediaEvent = _MediaDispatchEvent | nio.RoomMessageAudio | nio.RoomEncryptedAudio

type _DispatchPayloadBuilder = Callable[[MessageContext], Awaitable[DispatchPayload]]

type _TextDispatchEvent = nio.RoomMessageText | PreparedTextEvent

type _DispatchEvent = _TextDispatchEvent | _MediaDispatchEvent

type _MessageContext = MessageContext


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that has already passed ingress prechecks."""

    event: T
    requester_user_id: str


type _PrecheckedTextDispatchEvent = _PrecheckedEvent[_TextDispatchEvent]
type _PrecheckedMediaDispatchEvent = _PrecheckedEvent[_MediaDispatchEvent]
type _PrecheckedDispatchEvent = _PrecheckedTextDispatchEvent | _PrecheckedMediaDispatchEvent


class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    # Construction inputs
    agent_user: AgentMatrixUser
    storage_path: Path
    runtime_paths: RuntimePaths
    rooms: list[str]
    config_path: Path | None

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
    _dispatch_planner: DispatchPlanner
    _conversation_resolver: ConversationResolver
    _conversation_state_writer: ConversationStateWriter
    _delivery_gateway: DeliveryGateway
    _response_coordinator: ResponseCoordinator
    _tool_runtime_support: ToolRuntimeSupport
    _post_response_effects_support: PostResponseEffectsSupport
    _dispatch_hook_service: DispatchHookService
    _hook_context_support: HookContextSupport
    _knowledge_access_support: KnowledgeAccessSupport
    _deferred_overdue_task_drain_task: asyncio.Task[None] | None

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
        self.running = False
        self.last_sync_time = None
        self._last_sync_monotonic = None
        self._first_sync_done = False
        self._sync_shutting_down = False
        self._hook_registry_state = HookRegistryState(HookRegistry.empty())
        self._runtime_view = BotRuntimeState(
            client=None,
            config=config,
            enable_streaming=enable_streaming,
            orchestrator=None,
            event_cache=None,
        )
        self._deferred_overdue_task_drain_task = None
        self._init_runtime_components()

    def _init_runtime_components(self) -> None:
        """Initialize runtime-only helpers that depend on bound instance methods."""
        stable_matrix_id = MatrixID.from_agent(
            self.agent_name,
            self.config.get_domain(self.runtime_paths),
            self.runtime_paths,
        )
        self._coalescing_gate = CoalescingGate(
            dispatch_batch=self._dispatch_coalesced_batch,
            enabled=self._coalescing_enabled,
            debounce_seconds=self._coalescing_debounce_seconds,
            upload_grace_seconds=self._coalescing_upload_grace_seconds,
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
                matrix_id=stable_matrix_id,
                state_writer=self._conversation_state_writer,
            ),
        )
        self._inbound_turn_normalizer = InboundTurnNormalizer(
            InboundTurnNormalizerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                storage_path=self.storage_path,
                runtime_paths=self.runtime_paths,
                sender_domain=stable_matrix_id.domain,
                conversation_resolver=self._conversation_resolver,
            ),
        )
        self._delivery_gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=self._runtime_view,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                logger=self.logger,
                sender_domain=stable_matrix_id.domain,
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
            matrix_id=stable_matrix_id,
            resolver=self._conversation_resolver,
            hook_context=self._hook_context_support,
        )
        self._post_response_effects_support = PostResponseEffectsSupport(
            runtime=self._runtime_view,
            logger=self.logger,
            runtime_paths=self.runtime_paths,
            delivery_gateway=self._delivery_gateway,
        )
        self._response_coordinator = ResponseCoordinator(
            ResponseCoordinatorDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                stop_manager=self.stop_manager,
                runtime_paths=self.runtime_paths,
                storage_path=self.storage_path,
                agent_name=self.agent_name,
                matrix_full_id=stable_matrix_id.full_id,
                resolver=self._conversation_resolver,
                tool_runtime=self._tool_runtime_support,
                knowledge_access=self._knowledge_access_support,
                delivery_gateway=self._delivery_gateway,
                post_response_effects=self._post_response_effects_support,
                state_writer=self._conversation_state_writer,
            ),
        )
        self._dispatch_hook_service = DispatchHookService(
            hook_context=self._hook_context_support,
        )
        self._dispatch_planner = DispatchPlanner(
            DispatchPlannerDeps(
                runtime=self._runtime_view,
                logger=self.logger,
                handled_turn_ledger=self.handled_turn_ledger,
                runtime_paths=self.runtime_paths,
                storage_path=self.storage_path,
                agent_name=self.agent_name,
                matrix_id=stable_matrix_id,
                normalizer=self._inbound_turn_normalizer,
                resolver=self._conversation_resolver,
                delivery_gateway=self._delivery_gateway,
                response_coordinator=self._response_coordinator,
                hook_service=self._dispatch_hook_service,
                tool_runtime=self._tool_runtime_support,
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
    def event_cache(self) -> EventCache | None:
        """Return the advisory event cache."""
        return self._runtime_view.event_cache

    @event_cache.setter
    def event_cache(self, value: EventCache | None) -> None:
        """Update the advisory event cache."""
        self._runtime_view.event_cache = value

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
        return self._response_coordinator.in_flight_response_count

    @in_flight_response_count.setter
    def in_flight_response_count(self, value: int) -> None:
        """Update the number of active response lifecycles."""
        self._response_coordinator.in_flight_response_count = value

    @property
    def agent_name(self) -> str:
        """Get the agent name from username."""
        return self.agent_user.agent_name

    @cached_property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Get a logger with agent context bound."""
        return logger.bind(agent=emoji(self.agent_name))

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
        return self._response_coordinator.has_active_response_for_target(target)

    def _coalescing_enabled(self) -> bool:
        """Return whether live coalescing is enabled for this bot."""
        coalescing = self.config.defaults.coalescing
        enabled = coalescing.enabled
        return enabled if isinstance(enabled, bool) else False

    def _coalescing_debounce_seconds(self) -> float:
        """Return the configured live coalescing debounce window in seconds."""
        coalescing = self.config.defaults.coalescing
        debounce_ms = coalescing.debounce_ms
        if not isinstance(debounce_ms, int | float):
            return 0.0
        return max(float(debounce_ms), 0.0) / 1000

    def _coalescing_upload_grace_seconds(self) -> float:
        """Return the configured upload-grace window in seconds."""
        coalescing = self.config.defaults.coalescing
        upload_grace_ms = coalescing.upload_grace_ms
        if not isinstance(upload_grace_ms, int | float):
            return 0.0
        return max(float(upload_grace_ms), 0.0) / 1000

    def _mark_source_events_responded(
        self,
        handled_turn: HandledTurnState,
    ) -> None:
        """Mark one or more source events as handled by the same response."""
        record_handled_turn(self.handled_turn_ledger, handled_turn)

    def _dispatch_matrix_run_metadata(
        self,
        handled_turn: HandledTurnState,
    ) -> dict[str, Any] | None:
        """Build run metadata extras for one dispatch turn."""
        return matrix_run_metadata_for_handled_turn(handled_turn)

    def _response_history_scope_for_action(
        self,
        response_action: ResponseAction,
    ) -> HistoryScope | None:
        """Return the persisted history scope used by one response action."""
        if response_action.kind == "team":
            assert response_action.form_team is not None
            return self._conversation_state_writer.team_history_scope(response_action.form_team.eligible_members)
        if response_action.kind == "individual":
            return self._conversation_state_writer.history_scope()
        return None

    def _handled_turn_with_response_context(
        self,
        handled_turn: HandledTurnState,
        *,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
    ) -> HandledTurnState:
        """Attach the persisted regeneration context for one response."""
        return handled_turn.with_response_context(
            response_owner=self.agent_name,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    def _load_persisted_turn_metadata(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> PersistedTurnMetadata | None:
        """Load persisted run metadata for one edited turn when available."""
        return self._conversation_state_writer.load_persisted_turn_metadata(
            LoadPersistedTurnMetadataRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self._conversation_resolver.build_message_target,
            build_tool_execution_identity=self._tool_runtime_support.build_execution_identity,
        )

    async def _edit_regeneration_context(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        conversation_target: MessageTarget | None,
    ) -> MessageContext:
        """Return edit context, reusing the recorded thread root when available."""
        context = await self._conversation_resolver.extract_message_context(room, event)
        if conversation_target is None or conversation_target.resolved_thread_id is None:
            return context
        if context.thread_id == conversation_target.resolved_thread_id:
            return context
        assert self.client is not None
        return MessageContext(
            am_i_mentioned=context.am_i_mentioned,
            is_thread=True,
            thread_id=conversation_target.resolved_thread_id,
            thread_history=await self._conversation_resolver.fetch_thread_history(
                self.client,
                room.room_id,
                conversation_target.resolved_thread_id,
            ),
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            requires_full_thread_history=context.requires_full_thread_history,
        )

    def _remove_stale_runs_for_turn_record(
        self,
        *,
        turn_record: HandledTurnRecord,
        recorded_turn_context_available: bool,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> None:
        """Remove stale persisted runs using the recorded turn context when possible."""
        if (
            recorded_turn_context_available
            and turn_record.conversation_target is not None
            and turn_record.history_scope is not None
        ):
            self._conversation_state_writer.remove_stale_runs_for_turn_record(
                turn_record=turn_record,
                requester_user_id=requester_user_id,
                build_tool_execution_identity=self._tool_runtime_support.build_execution_identity,
                remove_run_by_event_id_fn=remove_run_by_event_id,
            )
            return
        self._remove_stale_runs_for_edited_message(
            room=room,
            thread_id=thread_id,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )

    def _has_newer_unresponded_in_thread(
        self,
        event: _TextDispatchEvent,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> bool:
        """Return True when a newer unresponded message from the same sender exists.

        Guards against duplicate replies during backlog replay: if the bot
        restarts and processes an older message while a newer one from the
        same sender is already visible in the thread, the older dispatch
        should be skipped.

        Automation events (scheduled tasks, hooks) are always independent
        actions and must never be suppressed.  Command messages (``!help``,
        etc.) are also excluded — they are handled separately and should
        not suppress earlier questions.
        """
        # Automation events (scheduled tasks, hooks) are independent — never suppress.
        # User-originated synthetics (coalesced batches, voice) must still be guarded.
        if isinstance(event, PreparedTextEvent) and is_automation_source_kind(event.source_kind_override or ""):
            return False
        event_ts = event.server_timestamp
        if event_ts is None or not thread_history:
            return False
        for msg in thread_history:
            if msg.sender != requester_user_id:
                continue
            if msg.timestamp is None or msg.timestamp <= event_ts:
                continue
            if msg.event_id == event.event_id:
                continue
            if self.handled_turn_ledger.has_responded(msg.event_id):
                continue
            # Commands are handled independently — skip them as suppression candidates.
            if msg.body and isinstance(msg.body, str) and command_parser.parse(msg.body.strip()) is not None:
                continue
            self.logger.info(
                "Skipping older message — newer unresponded message from same sender in thread",
                skipped_event_id=event.event_id,
                newer_event_id=msg.event_id,
            )
            return True
        return False

    def _should_skip_deep_synthetic_full_dispatch(
        self,
        *,
        event_id: str,
        envelope: MessageEnvelope,
    ) -> bool:
        """Return True when a deep synthetic hook relay must stop before dispatch."""
        ingress_policy = hook_ingress_policy(envelope)
        if ingress_policy.allow_full_dispatch:
            return False
        self.logger.debug(
            "Ignoring deep synthetic hook relay before command/response dispatch",
            event_id=event_id,
            source_kind=envelope.source_kind,
            hook_source=envelope.hook_source,
            message_received_depth=envelope.message_received_depth,
        )
        return True

    def _default_response_envelope(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        resolved_thread_id: str | None,
        requester_id: str,
        body: str,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
    ) -> MessageEnvelope:
        """Build the default outbound envelope when hooks did not supply one."""
        target = self._conversation_resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            thread_mode_override="room" if resolved_thread_id is None else None,
        )
        if target.resolved_thread_id != resolved_thread_id:
            target = target.with_thread_root(resolved_thread_id)
        return MessageEnvelope(
            source_event_id=reply_to_event_id,
            room_id=room_id,
            target=target,
            requester_id=requester_id,
            sender_id=requester_id,
            body=body,
            attachment_ids=tuple(attachment_ids or ()),
            mentioned_agents=(),
            agent_name=agent_name or self.agent_name,
            source_kind="message",
        )

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
            response = await cached_room_get_event(
                self.client,
                self.event_cache,
                room_id,
                normalized_target_event_id,
            )
            if isinstance(response, nio.RoomGetEventResponse):
                target_info = EventInfo.from_event(response.event.source)
                if target_info.thread_id:
                    thread_id = target_info.thread_id
                elif target_info.thread_id_from_edit:
                    thread_id = target_info.thread_id_from_edit
                elif not target_info.has_relations:
                    thread_history = await self._conversation_resolver.fetch_thread_history(
                        self.client,
                        room_id,
                        normalized_target_event_id,
                    )
                    if len(thread_history) > 1:
                        thread_id = normalized_target_event_id
            else:
                self.logger.debug(
                    "Failed to fetch reaction target event for hook context",
                    room_id=room_id,
                    target_event_id=normalized_target_event_id,
                    error=str(response),
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

    @cached_property
    def handled_turn_ledger(self) -> HandledTurnLedger:
        """Get or create the handled-turn ledger for this agent."""
        # Use the tracking subdirectory, not the root storage path
        tracking_dir = self.storage_path / "tracking"
        return HandledTurnLedger(self.agent_name, base_path=tracking_dir)

    @cached_property
    def stop_manager(self) -> StopManager:
        """Get or create the StopManager for this agent."""
        return StopManager()

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

        restored_tasks = await restore_scheduled_tasks(self.client, room_id, self.config, self.runtime_paths)
        if restored_tasks > 0:
            self.logger.info(f"Restored {restored_tasks} scheduled tasks in room {room_id}")

        restored_configs = await config_confirmation.restore_pending_changes(self.client, room_id)
        if restored_configs > 0:
            self.logger.info(f"Restored {restored_configs} pending config changes in room {room_id}")

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
        self.logger.info(f"Ensured Matrix user account: {self.agent_user.user_id}")

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
                    self.logger.info(f"Successfully set avatar for {self.agent_name}")
                else:
                    self.logger.warning(f"Failed to set avatar for {self.agent_name}")
            except Exception as e:
                self.logger.warning(f"Failed to set avatar: {e}")

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

    def seconds_since_last_sync_activity(self) -> float | None:
        """Return elapsed seconds since the last successful sync or loop start."""
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
            await self._cache_sync_timeline_events(_response)

        self._first_sync_done = True

        if first_sync_response:
            await self._emit_agent_lifecycle_event(EVENT_BOT_READY)

        if first_sync_response or has_deferred_overdue_tasks():
            self._maybe_start_deferred_overdue_task_drain()

    async def _on_sync_error(self, _response: nio.SyncError) -> None:
        """Update the watchdog clock on sync errors so it knows the loop is alive."""
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

    async def _initialize_event_cache(self) -> None:
        """Initialize the persistent Matrix event cache when enabled."""
        assert self.client is not None
        if not self.config.cache.enabled:
            return

        event_cache = EventCache(self.config.cache.resolve_db_path(self.runtime_paths))
        try:
            await event_cache.initialize()
        except Exception as exc:
            self.logger.warning("Failed to initialize event cache", error=str(exc))
            return

        self.event_cache = event_cache

    async def _close_event_cache(self) -> None:
        """Close the persistent Matrix event cache when present."""
        event_cache = self.event_cache
        self.event_cache = None
        if event_cache is None:
            return

        try:
            await event_cache.close()
        except Exception as exc:
            self.logger.warning("Failed to close event cache", error=str(exc))

    async def _cache_thread_event(
        self,
        room_id: str,
        event: nio.RoomMessageText,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append live thread events to the cache when the thread was already hydrated."""
        event_cache = self.event_cache
        if event_cache is None:
            return

        thread_id = event_info.thread_id
        if thread_id is None and event_info.is_edit and event_info.original_event_id is not None:
            thread_id = event_info.thread_id_from_edit or await event_cache.get_thread_id_for_event(
                room_id,
                event_info.original_event_id,
            )
        if thread_id is None:
            return

        server_timestamp = event.server_timestamp
        event_source = normalize_event_source_for_cache(
            event.source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=server_timestamp
            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
            else None,
        )

        try:
            await event_cache.append_event(room_id, thread_id, event_source)
        except Exception as exc:
            self.logger.warning(
                "Failed to append live thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event.event_id,
                error=str(exc),
            )

    async def _cache_redaction_event(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply live redactions to cached thread history when relevant."""
        event_cache = self.event_cache
        if event_cache is None:
            return

        thread_id = await event_cache.get_thread_id_for_event(room_id, event.redacts)
        server_timestamp = event.server_timestamp
        redaction_source = normalize_event_source_for_cache(
            event.source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=server_timestamp
            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
            else None,
        )

        try:
            redacted = await event_cache.redact_event(
                room_id,
                event.redacts,
                thread_id=thread_id,
                redaction_event=redaction_source,
            )
            if not redacted:
                return
        except Exception as exc:
            self.logger.warning(
                "Failed to apply live redaction to cache",
                room_id=room_id,
                thread_id=thread_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )

    async def _cache_sync_timeline_events(self, response: nio.SyncResponse) -> None:
        """Persist timeline events from sync so later point lookups can hit SQLite."""
        event_cache = self.event_cache
        if event_cache is None:
            return

        cached_events: list[tuple[str, str, dict[str, Any]]] = []
        for room_id, room_info in response.rooms.join.items():
            for event in room_info.timeline.events:
                if not isinstance(event, nio.Event):
                    continue
                if not isinstance(event.source, dict):
                    continue
                if not isinstance(event.event_id, str):
                    continue
                server_timestamp = event.server_timestamp
                cached_events.append(
                    (
                        event.event_id,
                        room_id,
                        normalize_event_source_for_cache(
                            event.source,
                            event_id=event.event_id,
                            sender=event.sender if isinstance(event.sender, str) else None,
                            origin_server_ts=server_timestamp
                            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
                            else None,
                        ),
                    ),
                )
        if not cached_events:
            return

        try:
            await event_cache.store_events_batch(cached_events)
        except Exception as exc:
            self.logger.warning("Failed to cache sync timeline events", error=str(exc), events=len(cached_events))

    async def start(self) -> None:
        """Start the agent bot with user account setup (but don't join rooms yet)."""
        await self.ensure_user_account()
        self.client = await login_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
        )
        await self._initialize_event_cache()
        await self._set_avatar_if_available()
        await self._set_presence_with_model_info()
        interactive.init_persistence(self.runtime_paths.storage_root)
        client = self.client
        assert client is not None

        # Register event callbacks - wrap them to run as background tasks
        # This ensures the sync loop is never blocked, allowing stop reactions to work
        client.add_event_callback(_create_task_wrapper(self._on_invite), nio.InviteEvent)  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
        client.add_event_callback(_create_task_wrapper(self._on_message), nio.RoomMessageText)
        client.add_event_callback(_create_task_wrapper(self._on_redaction), nio.RedactionEvent)
        client.add_event_callback(_create_task_wrapper(self._on_reaction), nio.ReactionEvent)

        # Register media callbacks on all agents (each agent handles its own routing)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageImage)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedImage)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageFile)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedFile)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageVideo)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedVideo)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageAudio)
        client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedAudio)
        client.add_response_callback(self._on_sync_response, nio.SyncResponse)  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
        client.add_response_callback(self._on_sync_error, nio.SyncError)  # ty: ignore[invalid-argument-type]

        self.running = True

        # Router bot has additional responsibilities
        if self.agent_name == ROUTER_AGENT_NAME:
            try:
                await cleanup_all_orphaned_bots(client, self.config, self.runtime_paths)
            except Exception as e:
                self.logger.warning(f"Could not cleanup orphaned bots (non-critical): {e}")

        # Note: Room joining is deferred until after invitations are handled
        self.logger.info(f"Agent setup complete: {self.agent_user.user_id}")
        await self._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

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
                logger.error(f"Failed to start agent {self.agent_name}: {exc}")  # noqa: TRY400
                raise
            logger.exception(f"Failed to start agent {self.agent_name}")
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
            await wait_for_background_tasks(timeout=5.0)  # 5 second timeout
            self.logger.info("Background tasks completed")
        except Exception as e:
            self.logger.warning(f"Some background tasks did not complete: {e}")

        if self.agent_name == ROUTER_AGENT_NAME:
            cleared_queued_tasks = clear_deferred_overdue_tasks()
            if cleared_queued_tasks > 0:
                self.logger.info("Cleared queued overdue scheduled tasks", count=cleared_queued_tasks)
            cancelled_tasks = await cancel_all_running_scheduled_tasks()
            if cancelled_tasks > 0:
                self.logger.info("Cancelled running scheduled tasks", count=cancelled_tasks)

        if self.client is not None:
            self.logger.warning("Client is not None in stop()")
            await self._close_event_cache()
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
            drained_count = await drain_deferred_overdue_tasks(self.client, self.config, self.runtime_paths)
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

    async def _coalescing_key_for_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        requester_user_id: str,
    ) -> CoalescingKey:
        """Return the sender/thread-scoped dispatch key for one event."""
        return (
            room.room_id,
            await self._conversation_resolver.coalescing_thread_id(room, event),
            requester_user_id,
        )

    async def _enqueue_for_dispatch(
        self,
        event: _DispatchEvent,
        room: nio.MatrixRoom,
        *,
        source_kind: str,
        requester_user_id: str | None = None,
    ) -> None:
        """Route one inbound event through the live coalescing gate."""
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_enter")
        effective_requester_user_id = requester_user_id or self._requester_user_id(
            sender=event.sender,
            source=event.source,
        )
        if self._is_trusted_internal_relay_event(event):
            if dispatch_timing is not None:
                dispatch_timing.note(coalescing_bypassed=True, coalescing_bypass_reason="trusted_internal_relay")
                dispatch_timing.mark("gate_exit")
            trusted_relay_event = cast("_TextDispatchEvent", event)
            await self._dispatch_text_message(
                room,
                trusted_relay_event,
                effective_requester_user_id,
            )
            return
        key = await self._coalescing_key_for_event(room, event, effective_requester_user_id)
        await self._coalescing_gate.enqueue(
            key,
            PendingEvent(
                event=event,
                room=room,
                source_kind=source_kind,
            ),
        )

    async def _dispatch_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Dispatch one flushed batch through the normal text pipeline."""
        dispatch_event = build_batch_dispatch_event(batch)
        dispatch_timing = get_dispatch_pipeline_timing(dispatch_event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        batch_coalescing_key = await self._coalescing_key_for_event(
            batch.room,
            batch.primary_event,
            batch.requester_user_id,
        )
        # The first room message opens the gate with thread_id=None, but dispatch
        # resolves that turn into a new thread rooted at the source event ID.
        canonical_key = (
            batch.room.room_id,
            self._conversation_resolver.build_message_target(
                room_id=batch.room.room_id,
                thread_id=batch_coalescing_key[1],
                reply_to_event_id=dispatch_event.event_id,
                event_source=dispatch_event.source,
            ).resolved_thread_id,
            batch.requester_user_id,
        )
        self._coalescing_gate.retarget(batch_coalescing_key, canonical_key)
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._dispatch_text_message(
                batch.room,
                dispatch_event,
                batch.requester_user_id,
                media_events=batch.media_events or None,
                handled_turn=HandledTurnState.create(
                    batch.source_event_ids,
                    source_event_prompts=batch.source_event_prompts,
                ),
            )

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._handle_message_inner(room, event)

    async def _handle_message_inner(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle one text message inside the per-turn thread-history cache scope."""
        self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
        assert self.client is not None
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        event_info = EventInfo.from_event(event.source)
        await self._cache_thread_event(room.room_id, event, event_info=event_info)
        if not isinstance(event.body, str):
            return
        # Skip messages that are still being streamed (use metadata, not text pattern)
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        if isinstance(event_content, dict) and event_content.get(STREAM_STATUS_KEY) in {
            STREAM_STATUS_PENDING,
            STREAM_STATUS_STREAMING,
        }:
            return

        if isinstance(event.source, dict):
            event.source[_RECEIVED_MONOTONIC_KEY] = time.monotonic()
        prechecked_event = self._precheck_dispatch_event(room, event, is_edit=event_info.is_edit)
        if prechecked_event is None:
            return

        if event_info.is_edit:
            await self._handle_message_edit(
                room,
                prechecked_event.event,
                event_info,
                requester_user_id=prechecked_event.requester_user_id,
            )
            return

        prepared_event = await self._inbound_turn_normalizer.resolve_text_event(
            TextNormalizationRequest(event=prechecked_event.event),
        )
        attach_dispatch_pipeline_timing(prepared_event.source, dispatch_timing)
        envelope = await self._conversation_resolver.build_dispatch_envelope(
            room=room,
            event=prepared_event,
            requester_user_id=prechecked_event.requester_user_id,
        )
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_event.event_id,
            envelope=envelope,
        ):
            return
        if should_handle_interactive_text_response(envelope):
            await interactive.handle_text_response(self.client, room, prepared_event, self.agent_name)
        if self._should_bypass_coalescing_for_active_thread_follow_up(envelope):
            if dispatch_timing is not None:
                dispatch_timing.mark("gate_enter")
                dispatch_timing.note(
                    coalescing_bypassed=True,
                    coalescing_bypass_reason="active_thread_follow_up",
                )
                dispatch_timing.mark("gate_exit")
            await self._dispatch_text_message(
                room,
                prepared_event,
                prechecked_event.requester_user_id,
            )
            return
        await self._enqueue_for_dispatch(
            prechecked_event.event,
            room,
            source_kind="message",
            requester_user_id=prechecked_event.requester_user_id,
        )

    def _should_bypass_coalescing_for_active_thread_follow_up(self, envelope: MessageEnvelope) -> bool:
        """Return whether one human thread follow-up should skip IN_FLIGHT coalescing."""
        if envelope.target.resolved_thread_id is None:
            return False
        if is_automation_source_kind(envelope.source_kind):
            return False
        if is_agent_id(envelope.sender_id, self.config, self.runtime_paths):
            return False
        return self._response_coordinator.has_active_response_for_target(envelope.target)

    async def _dispatch_text_message(  # noqa: C901, PLR0912, PLR0915
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent | _PrecheckedTextDispatchEvent,
        requester_user_id: str | None = None,
        *,
        media_events: list[_MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
    ) -> None:
        """Run the normal text/command dispatch pipeline for a prepared text event."""
        raw_event: _TextDispatchEvent
        if isinstance(event, _PrecheckedEvent):
            requester_user_id = event.requester_user_id
            raw_event = cast("_TextDispatchEvent", event.event)
        else:
            raw_event = event
        if requester_user_id is None:
            msg = "requester_user_id is required when dispatching a raw event"
            raise TypeError(msg)
        router_event: _DispatchEvent = raw_event
        event = await self._inbound_turn_normalizer.resolve_text_event(
            TextNormalizationRequest(event=raw_event),
        )
        dispatch_timing = get_dispatch_pipeline_timing(raw_event.source)
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        timing_scope_token = timing_scope_context.set(event.event_id[:20] if event.event_id else "unknown")
        try:
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_start")
            dispatch_started_at = time.monotonic()
            handled_turn = handled_turn or HandledTurnState.from_source_event_id(event.event_id)

            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_prepare_start")
            dispatch = await self._dispatch_planner.prepare_dispatch(
                room,
                event,
                requester_user_id,
                event_label="message",
                handled_turn=handled_turn,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_prepare_ready")
            if dispatch is None:
                return

            # Commands always dispatch and bypass thread-history suppression.
            command = command_parser.parse(event.body) if not media_events else None
            if command:
                if self.agent_name == ROUTER_AGENT_NAME:
                    await self._handle_command(
                        room,
                        _PrecheckedEvent(
                            event=event,
                            requester_user_id=requester_user_id,
                        ),
                        command,
                        source_envelope=dispatch.envelope,
                    )
                return

            if self._has_newer_unresponded_in_thread(
                event,
                requester_user_id,
                dispatch.context.thread_history,
            ):
                self._mark_source_events_responded(handled_turn)
                return
            if self._should_skip_deep_synthetic_full_dispatch(
                event_id=event.event_id,
                envelope=dispatch.envelope,
            ):
                return
            if dispatch.context.requires_full_thread_history:
                await self._conversation_resolver.hydrate_dispatch_context(room, event, dispatch.context)
            content = event.source.get("content") if isinstance(event.source, dict) else None
            message_attachment_ids = parse_attachment_ids_from_event_source(event.source)
            message_extra_content: dict[str, Any] = {}
            if message_attachment_ids:
                message_extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
            if isinstance(content, dict):
                original_sender = content.get(ORIGINAL_SENDER_KEY)
                if isinstance(original_sender, str):
                    message_extra_content[ORIGINAL_SENDER_KEY] = original_sender
                raw_audio_fallback = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY)
                if isinstance(raw_audio_fallback, bool) and raw_audio_fallback:
                    message_extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
            router_extra_content = dict(message_extra_content)
            if media_events and ORIGINAL_SENDER_KEY not in router_extra_content:
                router_extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_start")
            plan = await self._dispatch_planner.plan_dispatch(
                room,
                event,
                dispatch,
                extra_content=router_extra_content or None,
                media_events=media_events,
                handled_turn=handled_turn,
                router_event=media_events[0]
                if media_events and len(handled_turn.source_event_ids) == 1
                else router_event,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_ready")
            if plan.kind == "ignore":
                if plan.handled_turn_outcome is not None:
                    self._mark_source_events_responded(plan.handled_turn_outcome)
                return
            if plan.kind == "route":
                route_event = plan.router_event or event
                single_direct_media_route = (
                    isinstance(
                        route_event,
                        nio.RoomMessageFile
                        | nio.RoomEncryptedFile
                        | nio.RoomMessageVideo
                        | nio.RoomEncryptedVideo
                        | nio.RoomMessageImage
                        | nio.RoomEncryptedImage,
                    )
                    and media_events == [route_event]
                    and handled_turn.source_event_ids == (event.event_id,)
                )
                routing_kwargs: dict[str, Any] = {
                    "message": event.body if media_events else plan.router_message,
                    "requester_user_id": dispatch.requester_user_id,
                    "extra_content": plan.extra_content,
                }
                if plan.media_events is not None and not single_direct_media_route:
                    routing_kwargs["media_events"] = plan.media_events
                if (
                    plan.handled_turn is not None
                    and list(plan.handled_turn.source_event_ids) != [route_event.event_id]
                    and not single_direct_media_route
                ):
                    routing_kwargs["handled_turn"] = self._handled_turn_with_response_context(
                        plan.handled_turn,
                        history_scope=None,
                        conversation_target=dispatch.target,
                    )
                await self._handle_ai_routing(
                    room,
                    route_event,
                    dispatch.context.thread_history,
                    dispatch.context.thread_id,
                    **routing_kwargs,
                )
                return
            assert plan.response_action is not None
            handled_turn = self._handled_turn_with_response_context(
                handled_turn,
                history_scope=self._response_history_scope_for_action(plan.response_action),
                conversation_target=dispatch.target,
            )
            matrix_run_metadata = self._dispatch_matrix_run_metadata(handled_turn)

            async def build_payload(context: MessageContext) -> DispatchPayload:
                effective_thread_id = self._conversation_resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=context.thread_id,
                    reply_to_event_id=event.event_id,
                    event_source=event.source,
                ).resolved_thread_id
                media_attachment_ids: list[str] = []
                fallback_images: list[Image] | None = None
                if media_events:
                    media_result = await self._inbound_turn_normalizer.register_batch_media_attachments(
                        BatchMediaAttachmentRequest(
                            room_id=room.room_id,
                            thread_id=effective_thread_id,
                            media_events=media_events,
                        ),
                    )
                    media_attachment_ids = media_result.attachment_ids
                    fallback_images = media_result.fallback_images
                return await self._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                    DispatchPayloadWithAttachmentsRequest(
                        room_id=room.room_id,
                        prompt=event.body,
                        current_attachment_ids=merge_attachment_ids(
                            message_attachment_ids,
                            media_attachment_ids,
                        ),
                        thread_id=context.thread_id,
                        media_thread_id=effective_thread_id,
                        thread_history=context.thread_history,
                        fallback_images=fallback_images,
                    ),
                )

            await self._dispatch_planner.execute_response_action(
                room,
                event,
                dispatch,
                plan.response_action,
                build_payload,
                processing_log="Processing",
                dispatch_started_at=dispatch_started_at,
                handled_turn=handled_turn,
                matrix_run_metadata=matrix_run_metadata,
            )
        finally:
            timing_scope_context.reset(timing_scope_token)

    async def _on_redaction(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Keep cached thread history consistent when Matrix redactions arrive."""
        await self._cache_redaction_event(room.room_id, event)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._handle_reaction_inner(room, event)

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
            self.logger.debug(f"Ignoring reaction from unauthorized sender: {event.sender}")
            return

        if not self._dispatch_planner.can_reply_to_sender(event.sender):
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
            await self._handle_interactive_reaction_result(room, event, result)
            return

        await self._emit_reaction_received_hooks(
            room_id=room.room_id,
            event=event,
            correlation_id=event.event_id,
        )

    async def _handle_interactive_reaction_result(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        result: tuple[str, str | None],
    ) -> None:
        """Handle one validated interactive reaction selection."""
        assert self.client is not None
        selected_value, thread_id = result
        thread_history = (
            await self._conversation_resolver.fetch_thread_history(self.client, room.room_id, thread_id)
            if thread_id
            else []
        )

        ack_text = f"You selected: {event.key} {selected_value}\n\nProcessing your response..."
        # Matrix doesn't allow reply relations to events that already have relations (reactions).
        # In threads, omit reply_to_event_id; the thread_id ensures correct placement.
        ack_event_id = await self._send_response(
            room.room_id,
            None if thread_id else event.reacts_to,
            ack_text,
            thread_id,
        )
        if not ack_event_id:
            self.logger.error("Failed to send acknowledgment for reaction")
            return

        prompt = f"The user selected: {selected_value}"
        try:
            response_event_id = await self._generate_response(
                room_id=room.room_id,
                prompt=prompt,
                reply_to_event_id=event.reacts_to,
                thread_id=thread_id,
                thread_history=thread_history,
                existing_event_id=ack_event_id,
                existing_event_is_placeholder=True,
                user_id=event.sender,
            )
        except _SuppressedPlaceholderCleanupError:
            self.logger.warning(
                "Suppressed interactive acknowledgment cleanup failed",
                source_event_id=event.reacts_to,
                acknowledgment_event_id=ack_event_id,
            )
            return
        if response_event_id is not None:
            self._mark_source_events_responded(
                HandledTurnState.from_source_event_id(
                    event.reacts_to,
                    response_event_id=response_event_id,
                ),
            )

    async def _on_audio_media_message(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageAudio | nio.RoomEncryptedAudio],
    ) -> None:
        """Normalize audio into a synthetic text event and reuse text dispatch."""
        assert self.client is not None
        event = prechecked_event.event

        if is_agent_id(event.sender, self.config, self.runtime_paths):
            self.logger.debug(
                "Ignoring agent audio event for voice transcription",
                event_id=event.event_id,
                sender=event.sender,
            )
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return

        normalized_voice = await self._inbound_turn_normalizer.prepare_voice_event(
            VoiceNormalizationRequest(
                room=room,
                event=event,
            ),
        )
        if normalized_voice is None:
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return

        await self._maybe_send_visible_voice_echo(
            room,
            event,
            text=normalized_voice.event.body,
            thread_id=normalized_voice.effective_thread_id,
        )

        await self._enqueue_for_dispatch(
            normalized_voice.event,
            room,
            source_kind="voice",
            requester_user_id=prechecked_event.requester_user_id,
        )

    async def _maybe_send_visible_voice_echo(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
        *,
        text: str,
        thread_id: str | None,
    ) -> str | None:
        """Optionally post a display-only router echo for normalized audio."""
        if self.agent_name != ROUTER_AGENT_NAME or not self.config.voice.visible_router_echo:
            return None

        existing_visible_echo_event_id = self.handled_turn_ledger.get_visible_echo_event_id(event.event_id)
        if existing_visible_echo_event_id is not None:
            return existing_visible_echo_event_id

        visible_echo_event_id = await self._send_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=text,
            thread_id=thread_id,
            skip_mentions=True,
        )
        if visible_echo_event_id is not None:
            self.handled_turn_ledger.record_visible_echo(event.event_id, visible_echo_event_id)
        return visible_echo_event_id

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Handle image/file/video/audio events and dispatch media-aware responses."""
        async with self._conversation_resolver.turn_thread_cache_scope():
            await self._handle_media_message_inner(room, event)

    async def _handle_media_message_inner(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Handle one media event inside the per-turn thread-history cache scope."""
        assert self.client is not None

        prechecked_event = self._precheck_dispatch_event(room, event)
        if prechecked_event is None:
            return

        if await self._dispatch_special_media_as_text(room, prechecked_event):
            return
        event = prechecked_event.event
        await self._enqueue_for_dispatch(
            event,
            room,
            source_kind="image" if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage) else "media",
            requester_user_id=prechecked_event.requester_user_id,
        )

    async def _dispatch_special_media_as_text(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedMediaDispatchEvent,
    ) -> bool:
        """Handle media events that normalize into the text dispatch pipeline."""
        event = prechecked_event.event
        if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
            await self._on_audio_media_message(
                room,
                _PrecheckedEvent(
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                ),
            )
            return True
        if isinstance(event, nio.RoomMessageFile | nio.RoomEncryptedFile):
            return await self._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                ),
            )
        return False

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFile | nio.RoomEncryptedFile],
    ) -> bool:
        """Dispatch one sidecar-backed file preview through the normal text pipeline."""
        event = prechecked_event.event
        if not is_v2_sidecar_text_preview(event.source):
            return False

        prepared_text_event = await self._inbound_turn_normalizer.prepare_file_sidecar_text_event(event)
        assert prepared_text_event is not None
        assert self.client is not None
        envelope = await self._conversation_resolver.build_dispatch_envelope(
            room=room,
            event=prepared_text_event,
            requester_user_id=prechecked_event.requester_user_id,
        )
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_text_event.event_id,
            envelope=envelope,
        ):
            return True
        if should_handle_interactive_text_response(envelope):
            await interactive.handle_text_response(self.client, room, prepared_text_event, self.agent_name)
        await self._dispatch_text_message(
            room,
            _PrecheckedEvent(
                event=prepared_text_event,
                requester_user_id=prechecked_event.requester_user_id,
            ),
        )
        return True

    def _requester_user_id(
        self,
        *,
        sender: str,
        source: object,
    ) -> str:
        """Return the effective requester for reply-permission checks."""
        source_dict = cast("dict[str, Any] | None", source if isinstance(source, dict) else None)
        content = source_dict.get("content") if source_dict is not None else None
        if (
            sender == self.matrix_id.full_id
            and isinstance(content, dict)
            and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
        ):
            return content[ORIGINAL_SENDER_KEY]
        return get_effective_sender_id_for_reply_permissions(
            sender,
            source_dict,
            self.config,
            self.runtime_paths,
        )

    def _requester_user_id_for_event(
        self,
        event: CommandEvent,
    ) -> str:
        """Return the effective requester for per-user reply checks."""
        return self._requester_user_id(
            sender=event.sender,
            source=event.source,
        )

    def _is_trusted_internal_relay_event(self, event: _DispatchEvent) -> bool:
        """Return whether one agent-authored relay should bypass user-turn coalescing."""
        if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
            return False
        if extract_agent_name(event.sender, self.config, self.runtime_paths) is None:
            return False
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if not isinstance(content, dict):
            return False
        if content.get("com.mindroom.source_kind") == "scheduled":
            return False
        original_sender = content.get(ORIGINAL_SENDER_KEY)
        return isinstance(original_sender, str) and bool(original_sender)

    def _precheck_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent | _InboundMediaEvent,
        *,
        is_edit: bool = False,
    ) -> str | None:
        """Common early-exit checks shared by text/media/voice handlers.

        Returns the effective requester user ID when the event should be
        processed, or ``None`` when the event should be skipped.

        Checks (in order): self-authored, already processed (skipped for
        edits so restart recovery works), effective requester
        authorization, and per-agent reply permissions.
        """
        content = event.source.get("content") if isinstance(event.source, dict) else None
        source_kind = content.get("com.mindroom.source_kind") if isinstance(content, dict) else None
        requester_user_id = self._requester_user_id(
            sender=event.sender,
            source=event.source,
        )

        if requester_user_id == self.matrix_id.full_id and source_kind != "hook_dispatch":
            return None

        # Edits bypass the dedup check: if an edit is redelivered after a
        # restart the bot should still regenerate the response.
        if not is_edit and self.handled_turn_ledger.has_responded(event.event_id):
            return None

        if not is_authorized_sender(
            requester_user_id,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return None

        if not self._dispatch_planner.can_reply_to_sender(requester_user_id):
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return None

        return requester_user_id

    def _precheck_dispatch_event[T: _DispatchEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        is_edit: bool = False,
    ) -> _PrecheckedEvent[T] | None:
        """Return a typed prechecked event for ingress handlers.

        Raw Matrix handlers must call this once before dispatch so downstream
        helpers never need to guess whether requester resolution and sender
        gating already happened.
        """
        requester_user_id = self._precheck_event(room, event, is_edit=is_edit)
        if requester_user_id is None:
            return None
        return _PrecheckedEvent(event=event, requester_user_id=requester_user_id)

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedDispatchEvent,
        *,
        event_label: str,
        handled_turn: HandledTurnState,
    ) -> _PreparedDispatch | None:
        """Run common precheck/context/sender-gating for dispatch handlers."""
        return await self._dispatch_planner.prepare_dispatch(
            room,
            prechecked_event.event,
            prechecked_event.requester_user_id,
            event_label=event_label,
            handled_turn=handled_turn,
        )

    async def _resolve_text_dispatch_event(self, event: _TextDispatchEvent) -> PreparedTextEvent:
        """Return one canonical text event for hooks, routing, and command handling."""
        return await self._dispatch_planner.resolve_text_dispatch_event(event)

    async def _plan_dispatch(
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent,
        dispatch: _PreparedDispatch,
        *,
        extra_content: dict[str, Any] | None = None,
        media_events: list[_MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
        router_event: _DispatchEvent | None = None,
    ) -> DispatchPlan:
        """Return the explicit plan for one prepared dispatch."""
        return await self._dispatch_planner.plan_dispatch(
            room,
            event,
            dispatch,
            extra_content=extra_content,
            media_events=media_events,
            handled_turn=handled_turn,
            router_event=router_event,
        )

    async def _execute_dispatch_action(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        dispatch: _PreparedDispatch,
        action: _ResponseAction,
        payload_builder: _DispatchPayloadBuilder,
        *,
        processing_log: str,
        dispatch_started_at: float,
        handled_turn: HandledTurnState,
        matrix_run_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Execute resolved dispatch action and mark the source event responded."""
        await self._dispatch_planner.execute_response_action(
            room,
            event,
            dispatch,
            action,
            payload_builder,
            processing_log=processing_log,
            dispatch_started_at=dispatch_started_at,
            handled_turn=handled_turn,
            matrix_run_metadata=matrix_run_metadata,
        )

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

    async def _decide_team_for_sender(
        self,
        agents_in_thread: list[MatrixID],
        context: _MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
        *,
        available_agents_in_room: list[MatrixID] | None = None,
        materializable_agent_names: set[str] | None = None,
    ) -> TeamResolution:
        """Decide team formation using sender-visible candidates without losing explicit intent."""
        return await self._dispatch_planner.decide_team_for_sender(
            agents_in_thread,
            context,
            room,
            requester_user_id,
            message,
            is_dm,
            available_agents_in_room=available_agents_in_room,
            materializable_agent_names=materializable_agent_names,
        )

    async def _extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        full_history: bool = True,
    ) -> _MessageContext:
        """Extract message context, optionally using a lightweight thread snapshot."""
        return await self._conversation_resolver.extract_message_context(
            room,
            event,
            full_history=full_history,
        )

    @asynccontextmanager
    async def _turn_thread_cache_scope(self) -> AsyncIterator[None]:
        """Cache thread history for the lifetime of one message-handling turn."""
        async with self._conversation_resolver.turn_thread_cache_scope():
            yield

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
        return await self._response_coordinator.generate_team_response_helper(
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

    async def _run_cancellable_response(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        thinking_message: str | None = None,  # None means don't send thinking message
        existing_event_id: str | None = None,
        user_id: str | None = None,  # User ID for presence check
        run_id: str | None = None,
        target: MessageTarget | None = None,
    ) -> str | None:
        """Run a response generation function with cancellation support."""
        return await self._response_coordinator.run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            response_function=response_function,
            thinking_message=thinking_message,
            existing_event_id=existing_event_id,
            user_id=user_id,
            run_id=run_id,
            target=target,
        )

    def _request_with_resolved_thread_target(
        self,
        request: ResponseRequest,
        *,
        resolved_thread_id: str | None = None,
    ) -> ResponseRequest:
        """Apply an explicit resolved thread root to one response request."""
        if resolved_thread_id is None:
            return request
        resolved_target = (
            request.target
            or self._conversation_resolver.build_message_target(
                room_id=request.room_id,
                thread_id=request.thread_id,
                reply_to_event_id=request.reply_to_event_id,
            )
        ).with_thread_root(resolved_thread_id)
        return replace(request, target=resolved_target)

    async def _process_and_respond(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        resolved_thread_id: str | None = None,
        response_kind: str = "ai",
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    ) -> DeliveryResult:
        """Delegate non-streaming response execution to the coordinator."""
        resolved_request = self._request_with_resolved_thread_target(
            request,
            resolved_thread_id=resolved_thread_id,
        )
        return await self._response_coordinator.process_and_respond(
            resolved_request,
            run_id=run_id,
            response_kind=response_kind,
            compaction_outcomes_collector=compaction_outcomes_collector,
        )

    async def _send_skill_command_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Send a skill command response using a specific agent."""
        return await self._response_coordinator.send_skill_command_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            thread_history=thread_history,
            prompt=prompt,
            agent_name=agent_name,
            user_id=user_id,
            reply_to_event=reply_to_event,
            source_envelope=source_envelope,
        )

    async def _process_and_respond_streaming(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        resolved_thread_id: str | None = None,
        response_kind: str = "ai",
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    ) -> DeliveryResult:
        """Delegate streaming response execution to the coordinator."""
        resolved_request = self._request_with_resolved_thread_target(
            request,
            resolved_thread_id=resolved_thread_id,
        )
        return await self._response_coordinator.process_and_respond_streaming(
            resolved_request,
            run_id=run_id,
            response_kind=response_kind,
            compaction_outcomes_collector=compaction_outcomes_collector,
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
        received_monotonic: float | None = None,
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
            received_monotonic: Optional receive timestamp used for queued-message signaling.
            on_lifecycle_lock_acquired: Optional callback that runs after the response
                lifecycle lock is acquired and before response generation starts.

        Returns:
            Event ID of the response message, or None if failed

        """
        return await self._response_coordinator.generate_response(
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
                received_monotonic=received_monotonic,
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
        return await self._delivery_gateway.send_text(
            SendTextRequest(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                response_text=response_text,
                thread_id=thread_id,
                reply_to_event=reply_to_event,
                skip_mentions=skip_mentions,
                tool_trace=tool_trace,
                extra_content=extra_content,
                thread_mode_override=thread_mode_override,
                target=target,
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
                room_id=room_id,
                event_id=event_id,
                new_text=new_text,
                thread_id=thread_id,
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
        return True

    async def _handle_ai_routing(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        thread_history: Sequence[ResolvedVisibleMessage],
        thread_id: str | None = None,
        message: str | None = None,
        *,
        requester_user_id: str,
        extra_content: dict[str, Any] | None = None,
        media_events: list[_MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
    ) -> None:
        await self._dispatch_planner.execute_router_relay(
            room,
            event,
            thread_history,
            thread_id,
            message=message,
            requester_user_id=requester_user_id,
            extra_content=extra_content,
            media_events=media_events,
            handled_turn=handled_turn,
        )

    async def _handle_message_edit(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        event_info: EventInfo,
        *,
        requester_user_id: str,
    ) -> None:
        """Handle an edited message by regenerating the agent's response.

        Args:
            room: The Matrix room
            event: The edited message event
            event_info: Information about the edit event
            requester_user_id: Effective requester resolved during raw-event precheck

        """
        if not event_info.original_event_id:
            self.logger.debug("Edit event has no original event ID")
            return
        original_event_id = event_info.original_event_id

        # Skip edits from other agents
        sender_agent_name = extract_agent_name(event.sender, self.config, self.runtime_paths)
        if sender_agent_name:
            self.logger.debug(f"Ignoring edit from other agent: {sender_agent_name}")
            return

        # Known limitations for edit regeneration (ISSUE-110 Phase 5):
        # - Team-scoped responses regenerate with canonical history scope, not recorded scope.
        # - Router relay edits regenerate as AI responses instead of re-running router dispatch.
        # - Missing-ledger recovery loses response_owner/history_scope/conversation_target metadata.
        # These are acceptable because these edge cases do not occur in current usage.
        turn_record = self.handled_turn_ledger.get_turn_record(original_event_id)
        context = await self._edit_regeneration_context(
            room,
            event,
            conversation_target=turn_record.conversation_target if turn_record is not None else None,
        )
        persisted_turn_metadata = self._load_persisted_turn_metadata(
            room=room,
            thread_id=context.thread_id,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )
        if turn_record is None:
            if persisted_turn_metadata is None:
                self.logger.debug(
                    "No handled turn record found for edited message",
                    original_event_id=original_event_id,
                )
                return
            turn_record = HandledTurnRecord(
                anchor_event_id=persisted_turn_metadata.anchor_event_id,
                source_event_ids=persisted_turn_metadata.source_event_ids,
                response_event_id=persisted_turn_metadata.response_event_id,
                source_event_prompts=persisted_turn_metadata.source_event_prompts,
            )
        recorded_turn_context_available = bool(
            turn_record.conversation_target is not None and turn_record.history_scope is not None,
        )
        response_owner_missing = turn_record.response_owner is None
        if response_owner_missing and persisted_turn_metadata is not None:
            turn_record = replace(turn_record, response_owner=self.agent_name)
        response_event_id = (
            persisted_turn_metadata.response_event_id
            if persisted_turn_metadata is not None and persisted_turn_metadata.response_event_id is not None
            else turn_record.response_event_id
        )
        if response_event_id is None:
            self.logger.debug(f"No previous response found for edited message {original_event_id}")
            return
        regeneration_target = turn_record.conversation_target or self._conversation_resolver.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=turn_record.anchor_event_id,
        )
        regeneration_history_scope = turn_record.history_scope or self._conversation_state_writer.history_scope()
        regeneration_response_owner = turn_record.response_owner or self.agent_name
        if regeneration_response_owner != self.agent_name:
            self.logger.debug(
                "Ignoring edited message for turn owned by another entity",
                original_event_id=original_event_id,
                response_owner=regeneration_response_owner,
            )
            return
        needs_turn_record_backfill = (
            turn_record.response_event_id != response_event_id
            or response_owner_missing
            or turn_record.history_scope is None
            or turn_record.conversation_target is None
            or (
                turn_record.is_coalesced
                and turn_record.source_event_prompts is None
                and persisted_turn_metadata is not None
                and persisted_turn_metadata.source_event_prompts is not None
            )
        )
        coalesced_source_event_prompts = turn_record.source_event_prompts
        if (
            coalesced_source_event_prompts is None
            and persisted_turn_metadata is not None
            and persisted_turn_metadata.is_coalesced
        ):
            coalesced_source_event_prompts = persisted_turn_metadata.source_event_prompts

        self.logger.info(
            "Regenerating response for edited message",
            original_event_id=original_event_id,
            response_event_id=response_event_id,
        )

        edited_content, _ = await extract_edit_body(event.source, self.client)
        if edited_content is None:
            self.logger.debug("Edited message missing resolved body", event_id=event.event_id)
            return
        regeneration_handled_turn = HandledTurnState.create(
            turn_record.source_event_ids,
            response_event_id=response_event_id,
            response_owner=regeneration_response_owner,
            history_scope=regeneration_history_scope,
            conversation_target=regeneration_target,
        )
        regeneration_turn_record = replace(
            turn_record,
            response_event_id=response_event_id,
            response_owner=regeneration_response_owner,
            history_scope=regeneration_history_scope,
            conversation_target=regeneration_target,
        )
        if regeneration_turn_record.is_coalesced:
            if coalesced_source_event_prompts is None:
                self.logger.warning(
                    "Skipping edited coalesced turn regeneration without persisted source prompts",
                    original_event_id=original_event_id,
                    anchor_event_id=regeneration_turn_record.anchor_event_id,
                )
                return
            updated_prompt_map = dict(coalesced_source_event_prompts)
            updated_prompt_map[original_event_id] = edited_content
            rebuilt_prompt_parts: list[str] = []
            for source_event_id in regeneration_turn_record.source_event_ids:
                prompt_part = updated_prompt_map.get(source_event_id)
                if prompt_part is None:
                    self.logger.warning(
                        "Skipping edited coalesced turn regeneration with incomplete prompt map",
                        original_event_id=original_event_id,
                        missing_source_event_id=source_event_id,
                        anchor_event_id=regeneration_turn_record.anchor_event_id,
                    )
                    return
                rebuilt_prompt_parts.append(prompt_part)
            regeneration_prompt = coalesced_prompt(rebuilt_prompt_parts)
            regeneration_handled_turn = HandledTurnState.create(
                regeneration_turn_record.source_event_ids,
                response_event_id=response_event_id,
                source_event_prompts=updated_prompt_map,
                response_owner=regeneration_response_owner,
                history_scope=regeneration_history_scope,
                conversation_target=regeneration_target,
            )
            regeneration_turn_record = replace(regeneration_turn_record, source_event_prompts=updated_prompt_map)
            regeneration_matrix_run_metadata = self._dispatch_matrix_run_metadata(regeneration_handled_turn)
        else:
            regeneration_prompt = edited_content
            regeneration_matrix_run_metadata = None
        envelope = self._conversation_resolver.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=regeneration_target,
            body=edited_content,
            source_kind="edit",
        )
        ingress_policy = hook_ingress_policy(envelope)
        if await self._dispatch_hook_service.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
            policy=ingress_policy,
        ):
            self._mark_source_events_responded(regeneration_handled_turn)
            return

        if turn_record.response_owner is None:
            should_respond = should_agent_respond(
                agent_name=self.agent_name,
                am_i_mentioned=context.am_i_mentioned,
                is_thread=context.is_thread,
                room=room,
                thread_history=context.thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                mentioned_agents=context.mentioned_agents,
                has_non_agent_mentions=context.has_non_agent_mentions,
                sender_id=requester_user_id,
            )
            if not should_respond and not regeneration_turn_record.is_coalesced:
                self.logger.debug("Agent should not respond to edited message")
                if needs_turn_record_backfill:
                    self._mark_source_events_responded(regeneration_handled_turn)
                return

        # Generate new response
        regenerated_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=regeneration_prompt,
            reply_to_event_id=regeneration_turn_record.anchor_event_id,
            thread_id=regeneration_target.thread_id,
            target=regeneration_target,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            existing_event_is_placeholder=False,
            user_id=requester_user_id,
            response_envelope=envelope,
            correlation_id=event.event_id,
            matrix_run_metadata=regeneration_matrix_run_metadata,
            on_lifecycle_lock_acquired=lambda: self._remove_stale_runs_for_turn_record(
                turn_record=regeneration_turn_record,
                recorded_turn_context_available=recorded_turn_context_available,
                room=room,
                thread_id=context.thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )

        # Update the handled-turn ledger linkage for the edited source turn.
        if regenerated_event_id is not None:
            self._mark_source_events_responded(
                regeneration_handled_turn.with_response_event_id(regenerated_event_id),
            )
            self.logger.info("Successfully regenerated response for edited message")
        else:
            if needs_turn_record_backfill:
                self._mark_source_events_responded(regeneration_handled_turn)
            self.logger.info(
                "Suppressed regeneration left existing response unchanged",
                original_event_id=original_event_id,
                response_event_id=response_event_id,
            )

    def _remove_stale_runs_for_edited_message(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> None:
        """Remove persisted runs tied to the pre-edit message before regenerating."""
        self._conversation_state_writer.remove_stale_runs_for_edited_message(
            RemoveStaleRunsRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self._conversation_resolver.build_message_target,
            build_tool_execution_identity=self._tool_runtime_support.build_execution_identity,
            remove_run_by_event_id_fn=remove_run_by_event_id,
        )

    async def _handle_command(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedTextDispatchEvent,
        command: Command,
        *,
        source_envelope: MessageEnvelope | None = None,
    ) -> None:
        await self._dispatch_planner.execute_command(
            room=room,
            event=prechecked_event.event,
            requester_user_id=prechecked_event.requester_user_id,
            command=command,
            source_envelope=source_envelope,
        )


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
        received_monotonic: float | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate a team response instead of individual agent response."""
        del received_monotonic
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
        materializable_agent_names = self._dispatch_planner.materializable_agent_names()
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
            )

        media_inputs = media or MediaInputs()

        event_id = await self._generate_team_response_helper(
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
        if thread_id is not None and event_id is not None:
            self._post_response_effects_support.queue_thread_summary(
                room_id=room_id,
                thread_id=thread_id,
                message_count_hint=_thread_summary_message_count_hint(thread_history),
            )
        return event_id
