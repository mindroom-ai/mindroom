"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import nio
from agno.db.base import SessionType
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.hooks import (
    AfterResponseContext,
    AgentLifecycleContext,
    BeforeResponseContext,
    HookRegistry,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ReactionReceivedContext,
    ResponseDraft,
    ResponseResult,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
    emit_collect,
    emit_transform,
    render_enrichment_block,
    strip_enrichment_from_session_storage,
)
from mindroom.hooks.sender import HookMessageSender, send_hook_message
from mindroom.hooks.types import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_REACTION_RECEIVED,
)
from mindroom.matrix import image_handler
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
from mindroom.matrix.media import extract_media_caption
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.message_content import (
    extract_edit_body,
    is_v2_sidecar_text_preview,
    resolve_event_source_content,
    visible_body_from_event_source,
)
from mindroom.matrix.presence import (
    build_agent_status_message,
    is_user_online,
    set_presence_status,
    should_use_streaming,
)
from mindroom.matrix.reply_chain import ReplyChainCaches, derive_conversation_context, derive_conversation_target
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import (
    is_dm_room,
    leave_non_dm_rooms,
    resolve_room_aliases,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix.typing import typing_indicator
from mindroom.matrix.users import (
    AgentMatrixUser,
    create_agent_user,
    login_agent_user,
)
from mindroom.memory import store_conversation_memory
from mindroom.memory._prompting import strip_user_turn_time_prefix
from mindroom.memory.auto_flush import (
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)
from mindroom.stop import StopManager
from mindroom.streaming import (
    IN_PROGRESS_MARKER,
    ReplacementStreamingResponse,
    StreamingResponse,
    is_in_progress_message,
    send_streaming_response,
)
from mindroom.team_runtime_resolution import resolve_live_shared_agent_names
from mindroom.teams import (
    TeamIntent,
    TeamMode,
    TeamOutcome,
    TeamResolution,
    decide_team_formation,
    resolve_configured_team,
    select_model_for_team,
    team_response,
    team_response_stream,
)
from mindroom.thread_summary import maybe_generate_thread_summary
from mindroom.thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_configured_agents_for_room,
    has_multiple_non_agent_users_in_thread,
    should_agent_respond,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
    tool_execution_identity,
)

from . import constants, interactive, voice_handler
from .agents import create_agent, remove_run_by_event_id
from .ai import ai_response, stream_agent_response
from .attachment_media import resolve_attachment_media
from .attachments import (
    append_attachment_ids_prompt,
    merge_attachment_ids,
    parse_attachment_ids_from_event_source,
    parse_attachment_ids_from_thread_history,
    register_file_or_video_attachment,
    register_image_attachment,
    resolve_thread_attachment_ids,
)
from .authorization import (
    filter_agents_by_sender_permissions,
    get_available_agents_for_sender,
    get_effective_sender_id_for_reply_permissions,
    is_authorized_sender,
    is_sender_allowed_for_agent_reply,
)
from .background_tasks import create_background_task, wait_for_background_tasks
from .commands import config_confirmation
from .commands.handler import CommandEvent, CommandHandlerContext, _generate_welcome_message, handle_command
from .commands.parsing import Command, command_parser
from .constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    RuntimePaths,
    resolve_avatar_path,
)
from .error_handling import get_user_friendly_error_message
from .history.runtime import create_scope_session_storage, open_scope_storage
from .history.types import HistoryScope
from .knowledge.utils import (
    MultiKnowledgeVectorDb,
    ensure_request_knowledge_managers,
    get_agent_knowledge,
)
from .logging_config import emoji, get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client import (
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    build_threaded_edit_content,
    edit_message,
    fetch_thread_history,
    get_joined_rooms,
    get_latest_thread_event_id_if_needed,
    join_room,
    replace_visible_message,
    send_message,
)
from .media_inputs import MediaInputs
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    has_deferred_overdue_tasks,
    restore_scheduled_tasks,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    import structlog
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.knowledge.knowledge import Knowledge
    from agno.media import Image

    from mindroom.config.main import Config
    from mindroom.history import CompactionOutcome
    from mindroom.knowledge.manager import KnowledgeManager
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = ["AgentBot", "MultiKnowledgeVectorDb"]


# Constants
_SYNC_TIMEOUT_MS = 30000
_STOPPING_RESPONSE_TEXT = "⏹️ Stopping generation..."
_CANCELLED_RESPONSE_TEXT = "**[Response cancelled by user]**"
_COALESCING_EXEMPT_SOURCE_KINDS: frozenset[str] = frozenset({"scheduled", "hook"})


def _get_or_create_lock(locks: dict[object, asyncio.Lock], key: object, *, max_entries: int = 100) -> asyncio.Lock:
    """Return a cached lock for one key with bounded best-effort eviction."""
    lock = locks.get(key)
    if lock is not None:
        return lock
    if len(locks) >= max_entries:
        for candidate, candidate_lock in list(locks.items()):
            if len(locks) < max_entries:
                break
            if candidate_lock.locked():
                continue
            locks.pop(candidate, None)
    lock = asyncio.Lock()
    locks[key] = lock
    return lock


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


@dataclass(frozen=True)
class _ResponseAction:
    """Result of the shared team-formation / should-respond decision."""

    kind: Literal["skip", "team", "individual", "reject"]
    form_team: TeamResolution | None = None
    rejection_message: str | None = None


@dataclass(frozen=True)
class _RouterDispatchResult:
    """Whether router dispatch consumed the event and if display-only echoes count as handled."""

    handled: bool
    mark_visible_echo_responded: bool = False


def _should_skip_mentions(event_source: dict) -> bool:
    """Check if mentions in this message should be ignored for agent responses.

    This is used for messages like scheduling confirmations that contain mentions
    but should not trigger agent responses.

    Args:
        event_source: The Matrix event source dict

    Returns:
        True if mentions should be ignored, False otherwise

    """
    content = event_source.get("content", {})
    if not isinstance(content, dict):
        return False
    if bool(content.get("com.mindroom.skip_mentions", False)):
        return True

    new_content = content.get("m.new_content")
    return isinstance(new_content, dict) and bool(new_content.get("com.mindroom.skip_mentions", False))


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


@dataclass
class _MessageContext:
    """Context extracted from a Matrix message event."""

    am_i_mentioned: bool
    is_thread: bool
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    mentioned_agents: list[MatrixID]
    has_non_agent_mentions: bool
    requires_full_thread_history: bool = False


type _MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
    | nio.RoomMessageAudio
    | nio.RoomEncryptedAudio
)


@dataclass(frozen=True)
class _PreparedDispatch:
    """Common dispatch context reused across media handlers."""

    requester_user_id: str
    context: _MessageContext
    correlation_id: str
    envelope: MessageEnvelope


@dataclass(frozen=True)
class _DispatchPayload:
    """Dispatch prompt + optional media + attachment metadata."""

    prompt: str
    model_prompt: str | None = None
    media: MediaInputs = field(default_factory=MediaInputs)
    attachment_ids: list[str] | None = None


type _DispatchPayloadBuilder = Callable[[_MessageContext], Awaitable[_DispatchPayload]]


@dataclass(frozen=True)
class _ResponseDispatchResult:
    """Final send or edit outcome for one generated response."""

    event_id: str | None
    response_text: str
    delivery_kind: Literal["sent", "edited"] | None
    suppressed: bool = False
    option_map: dict[str, str] | None = None
    options_list: list[dict[str, str]] | None = None


@dataclass(frozen=True)
class _ResponseTarget:
    """Canonical thread target and persisted session scope for one response lifecycle."""

    resolved_thread_id: str | None
    delivery_thread_id: str | None
    session_id: str


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Prompt and tool runtime derived from one canonical response target."""

    model_prompt: str
    tool_context: ToolRuntimeContext | None
    execution_identity: ToolExecutionIdentity


class _SuppressedPlaceholderCleanupError(RuntimeError):
    """Raised when a suppressed placeholder cannot be removed safely."""


@dataclass(frozen=True)
class _PreparedHookedPayload:
    """Resolved dispatch payload after enrichment hooks run."""

    payload: _DispatchPayload
    envelope: MessageEnvelope
    strip_transient_enrichment_after_run: bool = False


@dataclass(frozen=True)
class _PreparedTextEvent:
    """Normalized inbound text event with canonical body/source for dispatch.

    This intentionally satisfies the ``CommandEvent`` protocol used by command handling.
    """

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]
    server_timestamp: int | None = None
    is_synthetic: bool = False


type _TextDispatchEvent = nio.RoomMessageText | _PreparedTextEvent

type _DispatchEvent = _TextDispatchEvent | _MediaDispatchEvent


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that has already passed ingress prechecks."""

    event: T
    requester_user_id: str


type _PrecheckedTextDispatchEvent = _PrecheckedEvent[_TextDispatchEvent]
type _PrecheckedDispatchEvent = _PrecheckedEvent[_DispatchEvent]
type _PrecheckedMediaDispatchEvent = _PrecheckedEvent[_MediaDispatchEvent]


def _is_coalescing_exempt_source_kind(event: _DispatchEvent) -> bool:
    """Return True when coalescing should be skipped for this event.

    Automation messages (scheduled tasks, hooks) are one-shot synthetic events
    that must never be coalesced — coalescing targets rapid human typing only.
    """
    if not isinstance(event.source, dict):
        return False
    content = event.source.get("content")
    if not isinstance(content, dict):
        return False
    source_kind = content.get("com.mindroom.source_kind")
    return isinstance(source_kind, str) and source_kind in _COALESCING_EXEMPT_SOURCE_KINDS


def _merge_response_extra_content(
    extra_content: dict[str, Any] | None,
    attachment_ids: list[str] | None,
) -> dict[str, Any] | None:
    """Merge optional attachment IDs into response metadata."""
    merged_extra_content = extra_content if extra_content is not None else {}
    if attachment_ids:
        merged_extra_content[ATTACHMENT_IDS_KEY] = attachment_ids
    return merged_extra_content if extra_content is not None or attachment_ids else None


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    _MATRIX_PROMPT_CONTEXT_MARKER = "[Matrix metadata for tool calls]"

    agent_user: AgentMatrixUser
    storage_path: Path
    config: Config
    runtime_paths: RuntimePaths
    rooms: list[str] = field(default_factory=list)
    config_path: Path | None = None

    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    enable_streaming: bool = field(default=True)  # Enable/disable streaming responses
    orchestrator: MultiAgentOrchestrator | None = field(default=None, init=False)  # Reference to orchestrator
    last_sync_time: datetime | None = field(default=None, init=False)
    _last_sync_monotonic: float | None = field(default=None, init=False)
    _first_sync_done: bool = field(default=False, init=False)
    _sync_shutting_down: bool = field(default=False, init=False)
    hook_registry: HookRegistry = field(default_factory=HookRegistry.empty, init=False)
    _reply_chain: ReplyChainCaches = field(default_factory=ReplyChainCaches, init=False)
    _response_lifecycle_locks: dict[tuple[str, str | None], asyncio.Lock] = field(
        default_factory=dict,
        init=False,
    )
    in_flight_response_count: int = field(default=0, init=False)
    _deferred_overdue_task_drain_task: asyncio.Task[None] | None = field(default=None, init=False)

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

    def _response_lifecycle_lock(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        *,
        resolved_thread_id: str | None = None,
    ) -> asyncio.Lock:
        """Return the per-thread lock that serializes one response lifecycle."""
        effective_resolved_thread_id = (
            resolved_thread_id
            if resolved_thread_id is not None
            else self._resolved_conversation_thread_id(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
            )
        )
        return _get_or_create_lock(
            cast("dict[object, asyncio.Lock]", self._response_lifecycle_locks),
            (room_id, effective_resolved_thread_id),
        )

    def _resolved_conversation_thread_id(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
    ) -> str | None:
        """Return the canonical conversation root for locks and persisted sessions."""
        return self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
        )

    def _conversation_session_id(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        resolved_thread_id: str | None = None,
    ) -> str:
        """Return the canonical persisted session ID for one response lifecycle."""
        return create_session_id(
            room_id,
            resolved_thread_id
            if resolved_thread_id is not None
            else self._resolved_conversation_thread_id(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
            ),
        )

    def _hook_base_kwargs(self, event_name: str, correlation_id: str) -> dict[str, Any]:
        """Return shared base fields for hook context construction."""
        return {
            "event_name": event_name,
            "plugin_name": "",
            "settings": {},
            "config": self.config,
            "runtime_paths": self.runtime_paths,
            "logger": self.logger.bind(event_name=event_name),
            "correlation_id": correlation_id,
            "message_sender": self._hook_message_sender(),
            "room_state_querier": build_hook_room_state_querier(self.client) if self.client is not None else None,
            "room_state_putter": build_hook_room_state_putter(self.client) if self.client is not None else None,
        }

    def _hook_message_sender(self) -> HookMessageSender | None:
        """Return the sender bound into hook contexts for this bot."""
        if self.orchestrator is not None:
            sender = self.orchestrator._hook_message_sender()
            if sender is not None:
                return sender
        if self.agent_name == ROUTER_AGENT_NAME and self.client is not None:
            return self._hook_send_message
        return None

    def _build_message_envelope(
        self,
        *,
        room_id: str,
        event: _DispatchEvent,
        requester_user_id: str,
        context: _MessageContext,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
        body: str | None = None,
        source_kind: str | None = None,
    ) -> MessageEnvelope:
        """Build the normalized inbound envelope consumed by message hooks."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        resolved_source_kind = source_kind
        if resolved_source_kind is None and isinstance(content, dict):
            source_kind_override = content.get("com.mindroom.source_kind")
            source_kind_sender_is_trusted = (isinstance(event, _PreparedTextEvent) and event.is_synthetic) or (
                extract_agent_name(event.sender, self.config, self.runtime_paths) is not None
            )
            if isinstance(source_kind_override, str) and source_kind_override and source_kind_sender_is_trusted:
                resolved_source_kind = source_kind_override
        if resolved_source_kind is None:
            if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
                resolved_source_kind = "voice"
            elif isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
                resolved_source_kind = "image"
            else:
                resolved_source_kind = "message"

        return MessageEnvelope(
            source_event_id=event.event_id,
            room_id=room_id,
            thread_id=context.thread_id,
            resolved_thread_id=self._resolve_reply_thread_id(
                context.thread_id,
                event.event_id,
                room_id=room_id,
                event_source=event.source,
            ),
            requester_id=requester_user_id,
            sender_id=event.sender,
            body=body or event.body,
            attachment_ids=tuple(attachment_ids or parse_attachment_ids_from_event_source(event.source)),
            mentioned_agents=tuple(
                agent_id.agent_name(self.config, self.runtime_paths) or agent_id.username
                for agent_id in context.mentioned_agents
            ),
            agent_name=agent_name or self.agent_name,
            source_kind=resolved_source_kind,
        )

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
        return MessageEnvelope(
            source_event_id=reply_to_event_id,
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            requester_id=requester_id,
            sender_id=requester_id,
            body=body,
            attachment_ids=tuple(attachment_ids or ()),
            mentioned_agents=(),
            agent_name=agent_name or self.agent_name,
            source_kind="message",
        )

    async def _emit_message_received_hooks(
        self,
        *,
        envelope: MessageEnvelope,
        correlation_id: str,
    ) -> bool:
        """Emit message:received and return whether hooks suppressed processing."""
        if envelope.source_kind == "hook":
            self.logger.debug(
                "Skipping message:received hooks for hook-originated automation message",
                event_id=envelope.source_event_id,
                room_id=envelope.room_id,
            )
            return False

        if not self.hook_registry.has_hooks(EVENT_MESSAGE_RECEIVED):
            return False

        context = MessageReceivedContext(
            **self._hook_base_kwargs(EVENT_MESSAGE_RECEIVED, correlation_id),
            envelope=envelope,
        )
        await emit(self.hook_registry, EVENT_MESSAGE_RECEIVED, context)
        return context.suppress

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
            response = await self.client.room_get_event(room_id, normalized_target_event_id)
            if isinstance(response, nio.RoomGetEventResponse):
                target_info = EventInfo.from_event(response.event.source)
                if target_info.thread_id:
                    thread_id = target_info.thread_id
                elif target_info.thread_id_from_edit:
                    thread_id = target_info.thread_id_from_edit
                elif not target_info.has_relations:
                    thread_history = await fetch_thread_history(self.client, room_id, normalized_target_event_id)
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
            **self._hook_base_kwargs(EVENT_REACTION_RECEIVED, correlation_id),
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
        context = AgentLifecycleContext(
            **self._hook_base_kwargs(event_name, f"{event_name}:{self.agent_name}:{uuid4().hex}"),
            entity_name=self.agent_name,
            entity_type=self._entity_type(),
            rooms=tuple(self.rooms),
            matrix_user_id=matrix_user_id,
            stop_reason=stop_reason,
        )
        await emit(self.hook_registry, event_name, context)

    def _resolve_reply_thread_id(
        self,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        *,
        room_id: str | None = None,
        event_source: dict[str, Any] | None = None,
        thread_mode_override: Literal["thread", "room"] | None = None,
    ) -> str | None:
        """Resolve the effective thread root for outgoing replies.

        In room mode this always returns ``None`` so callers send plain room
        messages and store room-level state. In thread mode, this prefers an
        existing thread ID and falls back to a safe root/reply target.
        """
        effective_thread_mode = thread_mode_override or self.config.get_entity_thread_mode(
            self.agent_name,
            self.runtime_paths,
            room_id=room_id,
        )
        if effective_thread_mode == "room":
            return None
        event_info = EventInfo.from_event(event_source)
        return thread_id or event_info.safe_thread_root or reply_to_event_id

    def _resolve_response_thread_root(
        self,
        thread_id: str | None,
        reply_to_event_id: str | None,
        *,
        room_id: str,
        response_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Return the canonical thread root for outbound response delivery."""
        if response_envelope is not None:
            return response_envelope.resolved_thread_id
        return self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)

    def _prepare_response_target(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        resolved_thread_id: str | None = None,
        response_envelope: MessageEnvelope | None = None,
    ) -> _ResponseTarget:
        """Compute the canonical thread target for one response lifecycle."""
        effective_resolved_thread_id = (
            resolved_thread_id
            if resolved_thread_id is not None
            else self._resolve_response_thread_root(
                thread_id,
                reply_to_event_id,
                room_id=room_id,
                response_envelope=response_envelope,
            )
        )
        return _ResponseTarget(
            resolved_thread_id=effective_resolved_thread_id,
            delivery_thread_id=(
                effective_resolved_thread_id
                if existing_event_id is None or existing_event_is_placeholder
                else thread_id
            ),
            session_id=self._conversation_session_id(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                resolved_thread_id=effective_resolved_thread_id,
            ),
        )

    @property
    def show_tool_calls(self) -> bool:
        """Whether to show tool call details inline in responses."""
        return self._show_tool_calls_for_agent(self.agent_name)

    def _show_tool_calls_for_agent(self, agent_name: str) -> bool:
        """Resolve tool-call visibility for a specific agent."""
        agent_config = self.config.agents.get(agent_name)
        if agent_config and agent_config.show_tool_calls is not None:
            return agent_config.show_tool_calls
        return self.config.defaults.show_tool_calls

    def _knowledge_for_agent(
        self,
        agent_name: str,
        *,
        request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    ) -> Knowledge | None:
        """Return the current knowledge assigned to one or more agent bases."""

        def _shared_manager(base_id: str) -> KnowledgeManager | None:
            if self.orchestrator is None:
                return None
            return self.orchestrator.knowledge_managers.get(base_id)

        return get_agent_knowledge(
            agent_name,
            self.config,
            self.runtime_paths,
            request_knowledge_managers=request_knowledge_managers,
            shared_manager_lookup=_shared_manager,
            on_missing_bases=lambda missing_base_ids: self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            ),
        )

    async def _ensure_request_knowledge_managers(
        self,
        agent_names: list[str],
        execution_identity: ToolExecutionIdentity,
    ) -> dict[str, KnowledgeManager]:
        """Ensure and collect managers needed for the current request scope."""
        try:
            return await ensure_request_knowledge_managers(
                agent_names,
                config=self.config,
                runtime_paths=self.runtime_paths,
                execution_identity=execution_identity,
            )
        except Exception:
            self.logger.exception(
                "Failed to initialize request-scoped knowledge managers",
                agent_names=agent_names,
            )
            return {}

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
        knowledge = self._knowledge_for_agent(self.agent_name)
        return create_agent(
            agent_name=self.agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            knowledge=knowledge,
            execution_identity=execution_identity,
            hook_registry=self.hook_registry,
        )

    @cached_property
    def response_tracker(self) -> ResponseTracker:
        """Get or create the response tracker for this agent."""
        # Use the tracking subdirectory, not the root storage path
        tracking_dir = self.storage_path / "tracking"
        return ResponseTracker(self.agent_name, base_path=tracking_dir)

    @cached_property
    def stop_manager(self) -> StopManager:
        """Get or create the StopManager for this agent."""
        return StopManager()

    def _active_response_event_ids(self, room_id: str) -> set[str]:
        """Return still-running response event IDs for this bot in the room."""
        return {
            event_id
            for event_id, tracked in self.stop_manager.tracked_messages.items()
            if tracked.room_id == room_id and not tracked.task.done()
        }

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

    async def start(self) -> None:
        """Start the agent bot with user account setup (but don't join rooms yet)."""
        await self.ensure_user_account()
        self.client = await login_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            self.agent_user,
            runtime_paths=self.runtime_paths,
        )
        await self._set_avatar_if_available()
        await self._set_presence_with_model_info()

        # Register event callbacks - wrap them to run as background tasks
        # This ensures the sync loop is never blocked, allowing stop reactions to work
        self.client.add_event_callback(_create_task_wrapper(self._on_invite), nio.InviteEvent)  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
        self.client.add_event_callback(_create_task_wrapper(self._on_message), nio.RoomMessageText)
        self.client.add_event_callback(_create_task_wrapper(self._on_reaction), nio.ReactionEvent)

        # Register media callbacks on all agents (each agent handles its own routing)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageImage)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedImage)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageFile)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedFile)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageVideo)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedVideo)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomMessageAudio)
        self.client.add_event_callback(_create_task_wrapper(self._on_media_message), nio.RoomEncryptedAudio)
        self.client.add_response_callback(self._on_sync_response, nio.SyncResponse)  # ty: ignore[invalid-argument-type]  # matrix-nio callback types are too strict here
        self.client.add_response_callback(self._on_sync_error, nio.SyncError)  # ty: ignore[invalid-argument-type]

        self.running = True

        # Router bot has additional responsibilities
        if self.agent_name == ROUTER_AGENT_NAME:
            try:
                await cleanup_all_orphaned_bots(self.client, self.config, self.runtime_paths)
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

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
        assert self.client is not None
        if not isinstance(event.body, str) or is_in_progress_message(event.body):
            return

        event_info = EventInfo.from_event(event.source)
        prechecked_event = self._precheck_dispatch_event(room, event, is_edit=event_info.is_edit)
        if prechecked_event is None:
            return

        # Handle edit events
        if event_info.is_edit:
            await self._handle_message_edit(
                room,
                prechecked_event.event,
                event_info,
                requester_user_id=prechecked_event.requester_user_id,
            )
            return

        prepared_event = await self._resolve_text_dispatch_event(prechecked_event.event)
        await interactive.handle_text_response(self.client, room, prepared_event, self.agent_name)
        await self._dispatch_text_message(
            room,
            _PrecheckedEvent(
                event=prepared_event,
                requester_user_id=prechecked_event.requester_user_id,
            ),
        )

    async def _dispatch_text_message(  # noqa: C901
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedTextDispatchEvent,
    ) -> None:
        """Run the normal text/command dispatch pipeline for a prepared text event."""
        event = await self._resolve_text_dispatch_event(prechecked_event.event)
        assert isinstance(event.body, str)
        dispatch_started_monotonic = time.monotonic()

        dispatch = await self._prepare_dispatch(
            room,
            _PrecheckedEvent(
                event=event,
                requester_user_id=prechecked_event.requester_user_id,
            ),
            event_label="message",
        )
        if dispatch is None:
            return

        # Router handles commands exclusively
        command = command_parser.parse(event.body)
        if command:
            if self.agent_name == ROUTER_AGENT_NAME:
                # Router always handles commands, even in single-agent rooms
                # Commands like !schedule, !help, etc. need to work regardless
                await self._handle_command(
                    room,
                    _PrecheckedEvent(
                        event=event,
                        requester_user_id=prechecked_event.requester_user_id,
                    ),
                    command,
                )
            return
        await self._hydrate_dispatch_context(room, event, dispatch.context)
        context_ready_monotonic = time.monotonic()
        if self._has_newer_unresponded_in_scope(event, dispatch.context):
            self.response_tracker.mark_responded(event.event_id)
            return

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

        action = await self._resolve_dispatch_action(
            room,
            event,
            dispatch,
            message_for_decision=event.body,
            extra_content=message_extra_content or None,
        )
        if action is None:
            return

        prompt_text = event.body

        async def build_payload(context: _MessageContext) -> _DispatchPayload:
            return await self._build_dispatch_payload_with_attachments(
                room_id=room.room_id,
                context=context,
                prompt=prompt_text,
                current_attachment_ids=message_attachment_ids,
                media_thread_id=context.thread_id,
            )

        await self._execute_dispatch_action(
            room,
            event,
            dispatch,
            action,
            build_payload,
            processing_log="Processing",
            dispatch_started_monotonic=dispatch_started_monotonic,
            context_ready_monotonic=context_ready_monotonic,
        )

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        assert self.client is not None

        # Check if sender is authorized to interact with agents
        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self.logger.debug(f"Ignoring reaction from unauthorized sender: {event.sender}")
            return

        # Check per-agent reply permissions before handling any reaction type
        # so disallowed senders cannot trigger stop confirmations, config
        # confirmations, or consume interactive questions.
        if not self._can_reply_to_sender(event.sender):
            self.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
            return

        # Check if this is a stop button reaction for a message currently being generated
        # Only process stop functionality if:
        # 1. The reaction is 🛑
        # 2. The sender is not an agent (users only)
        # 3. The message is currently being generated by this agent
        if event.key == "🛑":
            # Check if this is from a bot/agent
            sender_agent_name = extract_agent_name(event.sender, self.config, self.runtime_paths)
            # Only handle stop from users, not agents, and only if tracking this message
            if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
                self.logger.info(
                    "Stop requested for message",
                    message_id=event.reacts_to,
                    requested_by=event.sender,
                )
                # Remove the stop button immediately for user feedback
                await self.stop_manager.remove_stop_button(self.client, event.reacts_to)
                # Acknowledge immediately without claiming the task has fully exited yet.
                await self._send_response(room.room_id, event.reacts_to, _STOPPING_RESPONSE_TEXT, None)
                return
            # Message is not being generated - let the reaction be handled for other purposes
            # (e.g., interactive questions). Don't return here so it can fall through!
            # Agent reactions with 🛑 also fall through to other handlers

        # Then check if this is a config confirmation reaction
        pending_change = config_confirmation.get_pending_change(event.reacts_to)

        if pending_change and self.agent_name == ROUTER_AGENT_NAME:
            # Only router handles config confirmations
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
        thread_history = await fetch_thread_history(self.client, room.room_id, thread_id) if thread_id else []

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
            self.response_tracker.mark_responded(event.reacts_to, response_event_id)

    async def _build_dispatch_payload_with_attachments(
        self,
        *,
        room_id: str,
        context: _MessageContext,
        prompt: str,
        current_attachment_ids: list[str],
        media_thread_id: str | None,
        fallback_images: list[Image] | None = None,
    ) -> _DispatchPayload:
        """Build dispatch payload by merging thread/history attachment media."""
        assert self.client is not None
        thread_attachment_ids = (
            await resolve_thread_attachment_ids(
                self.client,
                self.storage_path,
                room_id=room_id,
                thread_id=context.thread_id,
            )
            if context.thread_id
            else []
        )
        history_attachment_ids = parse_attachment_ids_from_thread_history(context.thread_history)
        attachment_ids = merge_attachment_ids(
            current_attachment_ids,
            thread_attachment_ids,
            history_attachment_ids,
        )
        resolved_attachment_ids, attachment_audio, attachment_images, attachment_files, attachment_videos = (
            resolve_attachment_media(
                self.storage_path,
                attachment_ids,
                room_id=room_id,
                thread_id=media_thread_id,
            )
        )
        if fallback_images is not None and not attachment_images:
            attachment_images = fallback_images
        return _DispatchPayload(
            prompt=append_attachment_ids_prompt(prompt, resolved_attachment_ids),
            media=MediaInputs.from_optional(
                audio=attachment_audio,
                images=attachment_images,
                files=attachment_files,
                videos=attachment_videos,
            ),
            attachment_ids=resolved_attachment_ids or None,
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
            self.response_tracker.mark_responded(event.event_id)
            return

        event_info = EventInfo.from_event(event.source)
        _, thread_id, _ = await self._derive_conversation_context(room.room_id, event_info)
        effective_thread_id = self._resolve_reply_thread_id(
            thread_id,
            event.event_id,
            room_id=room.room_id,
            event_source=event.source,
        )
        prepared_voice = await voice_handler.prepare_voice_message(
            self.client,
            self.storage_path,
            room,
            event,
            self.config,
            runtime_paths=self.runtime_paths,
            sender_domain=self.matrix_id.domain,
            thread_id=effective_thread_id,
        )
        if prepared_voice is None:
            self.response_tracker.mark_responded(event.event_id)
            return

        await self._maybe_send_visible_voice_echo(
            room,
            event,
            text=prepared_voice.text,
            thread_id=effective_thread_id,
        )

        await self._dispatch_text_message(
            room,
            _PrecheckedEvent(
                event=_PreparedTextEvent(
                    sender=event.sender,
                    event_id=event.event_id,
                    body=prepared_voice.text,
                    source={
                        **prepared_voice.source,
                        "content": {
                            **prepared_voice.source.get("content", {}),
                            "com.mindroom.source_kind": "voice",
                        },
                    },
                    server_timestamp=None,
                    is_synthetic=True,
                ),
                requester_user_id=prechecked_event.requester_user_id,
            ),
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

        existing_visible_echo_event_id = self.response_tracker.get_visible_echo_event_id(event.event_id)
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
            self.response_tracker.mark_visible_echo_sent(event.event_id, visible_echo_event_id)
        return visible_echo_event_id

    async def _on_media_message(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Handle image/file/video/audio events and dispatch media-aware responses."""
        assert self.client is not None

        prechecked_event = self._precheck_dispatch_event(room, event)
        if prechecked_event is None:
            return

        if await self._dispatch_special_media_as_text(room, prechecked_event):
            return
        dispatch_started_monotonic = time.monotonic()

        event = prechecked_event.event

        is_image_event = isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage)
        default_caption = (
            "[Attached image]"
            if is_image_event
            else (
                "[Attached video]"
                if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo)
                else "[Attached file]"
            )
        )
        caption = extract_media_caption(event, default=default_caption)

        dispatch = await self._prepare_dispatch(
            room,
            prechecked_event,
            event_label="image" if is_image_event else "media",
        )
        if dispatch is None:
            return
        await self._hydrate_dispatch_context(room, event, dispatch.context)
        context_ready_monotonic = time.monotonic()
        action = await self._resolve_dispatch_action(
            room,
            event,
            dispatch,
            message_for_decision=event.body,
            router_message=caption,
            extra_content={ORIGINAL_SENDER_KEY: event.sender},
        )
        if action is None:
            return

        async def build_payload(context: _MessageContext) -> _DispatchPayload:
            client = self.client
            assert client is not None
            effective_thread_id = self._resolve_reply_thread_id(
                context.thread_id,
                event.event_id,
                room_id=room.room_id,
                event_source=event.source,
            )
            current_attachment_ids: list[str]
            fallback_images: list[Image] | None = None
            if is_image_event:
                assert isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage)
                image = await image_handler.download_image(client, event)
                if image is None:
                    msg = "Failed to download image"
                    raise RuntimeError(msg)
                attachment_record = await register_image_attachment(
                    client,
                    self.storage_path,
                    room_id=room.room_id,
                    thread_id=effective_thread_id,
                    event=event,
                    image_bytes=image.content,
                )
                current_attachment_ids = [attachment_record.attachment_id] if attachment_record is not None else []
                fallback_images = [image]
            else:
                assert isinstance(
                    event,
                    nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
                )
                attachment_record = await register_file_or_video_attachment(
                    client,
                    self.storage_path,
                    room_id=room.room_id,
                    thread_id=effective_thread_id,
                    event=event,
                )
                if attachment_record is None:
                    msg = "Failed to register media attachment"
                    raise RuntimeError(msg)
                current_attachment_ids = [attachment_record.attachment_id]
            return await self._build_dispatch_payload_with_attachments(
                room_id=room.room_id,
                context=context,
                prompt=caption,
                current_attachment_ids=current_attachment_ids,
                media_thread_id=effective_thread_id,
                fallback_images=fallback_images,
            )

        await self._execute_dispatch_action(
            room,
            event,
            dispatch,
            action,
            build_payload,
            processing_log="Processing image" if is_image_event else "Processing media message",
            dispatch_started_monotonic=dispatch_started_monotonic,
            context_ready_monotonic=context_ready_monotonic,
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

    async def _register_routed_attachment(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        event: _DispatchEvent,
    ) -> str | None:
        """Register a routed media event and return its attachment ID when available."""
        if isinstance(
            event,
            nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
        ):
            assert self.client is not None
            attachment_record = await register_file_or_video_attachment(
                self.client,
                self.storage_path,
                room_id=room_id,
                thread_id=thread_id,
                event=event,
            )
            if attachment_record is None:
                self.logger.error("Failed to register routed media attachment", event_id=event.event_id)
                return None
            return attachment_record.attachment_id

        if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
            assert self.client is not None
            attachment_record = await register_image_attachment(
                self.client,
                self.storage_path,
                room_id=room_id,
                thread_id=thread_id,
                event=event,
            )
            if attachment_record is None:
                self.logger.error("Failed to register routed image attachment", event_id=event.event_id)
                return None
            return attachment_record.attachment_id

        return None

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFile | nio.RoomEncryptedFile],
    ) -> bool:
        """Dispatch one sidecar-backed file preview through the normal text pipeline."""
        event = prechecked_event.event
        if not is_v2_sidecar_text_preview(event.source):
            return False

        prepared_text_event = await self._prepare_file_sidecar_text_event(event)
        assert prepared_text_event is not None
        assert self.client is not None
        await interactive.handle_text_response(self.client, room, prepared_text_event, self.agent_name)
        await self._dispatch_text_message(
            room,
            _PrecheckedEvent(
                event=prepared_text_event,
                requester_user_id=prechecked_event.requester_user_id,
            ),
        )
        return True

    async def _prepare_file_sidecar_text_event(
        self,
        event: nio.RoomMessageFile | nio.RoomEncryptedFile,
    ) -> _PreparedTextEvent | None:
        """Return a prepared text event when a file event is really a long-text preview."""
        if not is_v2_sidecar_text_preview(event.source):
            return None

        assert self.client is not None
        resolved_source = await resolve_event_source_content(event.source, self.client)
        return _PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=visible_body_from_event_source(resolved_source, event.body),
            source=resolved_source,
            server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )

    async def _derive_conversation_context(
        self,
        room_id: str,
        event_info: EventInfo,
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage]]:
        """Derive conversation context from threads or reply chains."""
        assert self.client is not None
        is_thread, thread_id, thread_history = await derive_conversation_context(
            self.client,
            room_id,
            event_info,
            self._reply_chain,
            self.logger,
            fetch_thread_history,
        )
        return is_thread, thread_id, thread_history

    async def _derive_conversation_target(
        self,
        room_id: str,
        event_info: EventInfo,
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage], bool]:
        """Derive dispatch target metadata without reconstructing preview history."""
        assert self.client is not None
        is_thread, thread_id, thread_history, requires_full_thread_history = await derive_conversation_target(
            self.client,
            room_id,
            event_info,
            self._reply_chain,
            self.logger,
        )
        return is_thread, thread_id, thread_history, requires_full_thread_history

    def _requester_user_id_for_event(
        self,
        event: CommandEvent,
    ) -> str:
        """Return the effective requester for per-user reply checks."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if (
            event.sender == self.matrix_id.full_id
            and isinstance(content, dict)
            and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
        ):
            return content[ORIGINAL_SENDER_KEY]
        return get_effective_sender_id_for_reply_permissions(
            event.sender,
            event.source,
            self.config,
            self.runtime_paths,
        )

    def _precheck_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        is_edit: bool = False,
    ) -> str | None:
        """Common early-exit checks shared by text/media/voice handlers.

        Returns the effective requester user ID when the event should be
        processed, or ``None`` when the event should be skipped.

        Checks (in order): self-authored, already processed (skipped for
        edits so restart recovery works), sender authorization, and
        per-agent reply permissions.
        """
        requester_user_id = self._requester_user_id_for_event(event)

        if requester_user_id == self.matrix_id.full_id:
            return None

        # Edits bypass the dedup check: if an edit is redelivered after a
        # restart the bot should still regenerate the response.
        if not is_edit and self.response_tracker.has_responded(event.event_id):
            return None

        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            self.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self.response_tracker.mark_responded(event.event_id)
            return None

        if not self._can_reply_to_sender(requester_user_id):
            self.response_tracker.mark_responded(event.event_id)
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

    def _has_newer_unresponded_in_scope(
        self,
        event: _DispatchEvent,
        context: _MessageContext,
    ) -> bool:
        """Return True if a newer unresponded message from the same sender exists.

        Compares raw ``event.sender`` against thread_history ``sender`` fields.
        When True the caller should ``mark_responded`` and skip AI generation;
        the latest message will pick up earlier ones via unseen-message context.

        Only considers newer messages that look like normal text (not ``!``
        commands), because commands exit early in dispatch without generating
        an AI response — coalescing against them would permanently drop the
        older message.

        Limitations (graceful degradation to current behaviour):
        - Room-mode (no thread_history): returns False.
        - Media / voice messages: not coalesced (text-only).
        - Race condition: if the newer task completes before the older task
          reaches this check, the older task proceeds normally (duplicate
          reply, same as pre-coalescing behaviour).
        """
        if not context.thread_history:
            return False

        current_ts = self._coalescing_candidate_timestamp(event)
        if current_ts is None:
            return False

        for msg in context.thread_history:
            if msg.event_id == event.event_id:
                continue
            newer_event_id = self._coalescing_replacement_event_id(
                msg,
                sender=event.sender,
                current_ts=current_ts,
            )
            if newer_event_id is None:
                continue
            self.logger.info(
                "Coalescing older message; newer unresponded message exists",
                event_id=event.event_id,
                coalesced_event_id=newer_event_id,
            )
            return True

        return False

    def _coalescing_candidate_timestamp(self, event: _DispatchEvent) -> int | None:
        if isinstance(event, _PreparedTextEvent):
            if event.is_synthetic:
                return None
            current_ts = event.server_timestamp
            if current_ts is None:
                return None
        else:
            current_ts = event.server_timestamp
        if not isinstance(current_ts, int):
            return None
        # Automation messages (scheduled tasks, hooks) are one-shot synthetic events
        # that must never be coalesced — coalescing targets rapid human typing only.
        if _is_coalescing_exempt_source_kind(event):
            return None
        return current_ts

    def _coalescing_replacement_event_id(
        self,
        msg: ResolvedVisibleMessage,
        *,
        sender: str,
        current_ts: int,
    ) -> str | None:
        event_id = msg.event_id
        if not isinstance(event_id, str):
            return None
        if msg.sender != sender:
            return None
        msg_ts = msg.timestamp
        if not isinstance(msg_ts, int) or msg_ts <= current_ts:
            return None
        # Skip commands — they exit early without generating an AI response,
        # so coalescing against them would permanently lose the older message.
        msg_body = msg.body or ""
        if isinstance(msg_body, str) and msg_body.lstrip().startswith("!"):
            return None
        if self.response_tracker.has_responded(event_id):
            return None
        return event_id

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedDispatchEvent,
        *,
        event_label: str,
    ) -> _PreparedDispatch | None:
        """Run common precheck/context/sender-gating for dispatch handlers."""
        event = prechecked_event.event
        effective_requester_user_id = prechecked_event.requester_user_id

        context = await self._extract_dispatch_context(room, event)
        correlation_id = event.event_id
        envelope = self._build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=effective_requester_user_id,
            context=context,
        )
        if await self._emit_message_received_hooks(
            envelope=envelope,
            correlation_id=correlation_id,
        ):
            self.response_tracker.mark_responded(event.event_id)
            return None

        sender_agent_name = extract_agent_name(effective_requester_user_id, self.config, self.runtime_paths)
        if sender_agent_name and not context.am_i_mentioned and envelope.source_kind != "hook_dispatch":
            self.logger.debug(f"Ignoring {event_label} from other agent (not mentioned)")
            return None

        return _PreparedDispatch(
            requester_user_id=effective_requester_user_id,
            context=context,
            correlation_id=correlation_id,
            envelope=envelope,
        )

    async def _resolve_text_dispatch_event(self, event: _TextDispatchEvent) -> _PreparedTextEvent:
        """Return one canonical text event for hooks, routing, and command handling."""
        if isinstance(event, _PreparedTextEvent):
            return event

        assert self.client is not None
        resolved_source = await resolve_event_source_content(event.source, self.client)
        return _PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=visible_body_from_event_source(resolved_source, event.body),
            source=resolved_source,
            server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )

    async def _resolve_dispatch_action(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        dispatch: _PreparedDispatch,
        *,
        message_for_decision: str,
        router_message: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> _ResponseAction | None:
        """Resolve routing + team/individual/skip action for a prepared dispatch."""
        if dispatch.context.requires_full_thread_history:
            msg = "dispatch action resolution requires hydrated thread history"
            raise RuntimeError(msg)
        router_result = await self._handle_router_dispatch(
            room,
            event,
            dispatch.context,
            dispatch.requester_user_id,
            message=router_message,
            extra_content=extra_content,
        )
        if router_result.handled:
            visible_router_echo_event_id = self.response_tracker.get_visible_echo_event_id(event.event_id)
            if (
                router_result.mark_visible_echo_responded
                and visible_router_echo_event_id is not None
                and not self.response_tracker.has_responded(event.event_id)
            ):
                self.response_tracker.mark_responded(event.event_id, visible_router_echo_event_id)
            return None

        assert self.client is not None
        dm_room = await is_dm_room(self.client, room.room_id)
        action = await self._resolve_response_action(
            dispatch.context,
            room,
            dispatch.requester_user_id,
            message_for_decision,
            dm_room,
        )
        if action.kind == "skip":
            return None
        return action

    async def _execute_dispatch_action(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        dispatch: _PreparedDispatch,
        action: _ResponseAction,
        payload_builder: _DispatchPayloadBuilder,
        *,
        processing_log: str,
        dispatch_started_monotonic: float,
        context_ready_monotonic: float,
    ) -> None:
        """Execute resolved dispatch action and mark the source event responded."""
        if action.kind == "reject":
            assert action.rejection_message is not None
            response_event_id = await self._send_response(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text=action.rejection_message,
                thread_id=dispatch.context.thread_id,
            )
            if response_event_id is not None:
                self.response_tracker.mark_responded(event.event_id, response_event_id)
            return

        if not dispatch.context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        response_target = self._prepare_response_target(
            room_id=room.room_id,
            thread_id=dispatch.context.thread_id,
            reply_to_event_id=event.event_id,
            resolved_thread_id=dispatch.envelope.resolved_thread_id,
            response_envelope=dispatch.envelope,
        )
        placeholder_text = "Thinking..."
        target_member_names: tuple[str, ...] | None = None
        if action.kind == "team":
            placeholder_text = "🤝 Team Response: Thinking..."
            assert action.form_team is not None
            assert action.form_team.mode is not None
            target_member_names = tuple(
                member.agent_name(self.config, self.runtime_paths) or member.username
                for member in action.form_team.eligible_members
            )

        placeholder_event_id = await self._send_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=f"{placeholder_text} {IN_PROGRESS_MARKER}",
            thread_id=response_target.delivery_thread_id,
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        )
        placeholder_ready_monotonic = time.monotonic()

        try:
            payload = await payload_builder(dispatch.context)
            prepared_payload = await self._apply_message_enrichment(
                dispatch,
                payload,
                target_entity_name=self.agent_name,
                target_member_names=target_member_names,
            )
            payload_ready_monotonic = time.monotonic()
        except Exception as error:
            response_event_id = await self._finalize_dispatch_failure(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_target=response_target,
                placeholder_event_id=placeholder_event_id,
                error=error,
            )
            if response_event_id is not None:
                self.response_tracker.mark_responded(event.event_id, response_event_id)
            return

        self._log_dispatch_latency(
            event_id=event.event_id,
            action_kind=action.kind,
            placeholder_event_id=placeholder_event_id,
            dispatch_started_monotonic=dispatch_started_monotonic,
            placeholder_ready_monotonic=placeholder_ready_monotonic,
            context_ready_monotonic=context_ready_monotonic,
            payload_ready_monotonic=payload_ready_monotonic,
        )

        self.logger.info(processing_log, event_id=event.event_id)
        try:
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                response_event_id = await self._generate_team_response_helper(
                    room_id=room.room_id,
                    reply_to_event_id=event.event_id,
                    thread_id=dispatch.context.thread_id,
                    payload=prepared_payload.payload,
                    team_agents=action.form_team.eligible_members,
                    team_mode=action.form_team.mode,
                    thread_history=dispatch.context.thread_history,
                    requester_user_id=dispatch.requester_user_id,
                    existing_event_id=placeholder_event_id,
                    existing_event_is_placeholder=placeholder_event_id is not None,
                    response_envelope=prepared_payload.envelope,
                    strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                    correlation_id=dispatch.correlation_id,
                )
            else:
                response_event_id = await self._generate_response(
                    room_id=room.room_id,
                    prompt=prepared_payload.payload.prompt,
                    reply_to_event_id=event.event_id,
                    thread_id=dispatch.context.thread_id,
                    thread_history=dispatch.context.thread_history,
                    user_id=dispatch.requester_user_id,
                    media=prepared_payload.payload.media,
                    attachment_ids=prepared_payload.payload.attachment_ids,
                    existing_event_id=placeholder_event_id,
                    existing_event_is_placeholder=placeholder_event_id is not None,
                    model_prompt=prepared_payload.payload.model_prompt,
                    strip_transient_enrichment_after_run=prepared_payload.strip_transient_enrichment_after_run,
                    response_envelope=prepared_payload.envelope,
                    correlation_id=dispatch.correlation_id,
                )
        except _SuppressedPlaceholderCleanupError:
            self.logger.warning(
                "Suppressed placeholder cleanup failed",
                source_event_id=event.event_id,
                placeholder_event_id=placeholder_event_id,
                correlation_id=dispatch.correlation_id,
            )
            return
        if response_event_id is not None:
            self.response_tracker.mark_responded(event.event_id, response_event_id)

    async def _finalize_dispatch_failure(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        response_target: _ResponseTarget,
        placeholder_event_id: str | None,
        error: Exception,
    ) -> str | None:
        """Convert post-placeholder setup failures into a visible terminal message."""
        error_text = get_user_friendly_error_message(error, self.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}
        if placeholder_event_id is None:
            return await self._send_response(
                room_id,
                reply_to_event_id,
                error_text,
                response_target.delivery_thread_id,
                extra_content=terminal_extra_content,
            )

        placeholder_updated = await self._edit_message(
            room_id,
            placeholder_event_id,
            error_text,
            response_target.delivery_thread_id,
            extra_content=terminal_extra_content,
        )
        if placeholder_updated:
            return placeholder_event_id

        return await self._send_response(
            room_id,
            reply_to_event_id,
            error_text,
            response_target.delivery_thread_id,
            extra_content=terminal_extra_content,
        )

    def _log_dispatch_latency(
        self,
        *,
        event_id: str,
        action_kind: str,
        placeholder_event_id: str | None,
        dispatch_started_monotonic: float,
        placeholder_ready_monotonic: float,
        context_ready_monotonic: float,
        payload_ready_monotonic: float,
    ) -> None:
        """Emit startup latency metrics for dispatch decisions that will respond."""
        self.logger.info(
            "Response startup latency",
            event_id=event_id,
            action_kind=action_kind,
            placeholder_event_id=placeholder_event_id,
            context_hydration_ms=round((context_ready_monotonic - dispatch_started_monotonic) * 1000, 1),
            placeholder_visible_ms=round((placeholder_ready_monotonic - context_ready_monotonic) * 1000, 1),
            payload_hydration_ms=round((payload_ready_monotonic - placeholder_ready_monotonic) * 1000, 1),
            startup_total_ms=round((payload_ready_monotonic - dispatch_started_monotonic) * 1000, 1),
        )

    def _can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this entity may reply to *sender_id*."""
        return is_sender_allowed_for_agent_reply(sender_id, self.agent_name, self.config, self.runtime_paths)

    def _materializable_agent_names(self) -> set[str] | None:
        """Return live shared agent names that can currently answer."""
        if self.orchestrator is None:
            return None
        return resolve_live_shared_agent_names(self.orchestrator, config=self.config)

    def _filter_materializable_agents(
        self,
        agent_ids: list[MatrixID],
        materializable_agent_names: set[str] | None,
    ) -> list[MatrixID]:
        """Keep only agents that can currently be materialized."""
        if materializable_agent_names is None:
            return agent_ids
        return [
            agent_id
            for agent_id in agent_ids
            if (agent_id.agent_name(self.config, self.runtime_paths) or agent_id.username) in materializable_agent_names
        ]

    def _response_owner_for_team_resolution(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> MatrixID | None:
        """Return the single live bot that should surface this resolution."""
        if form_team.outcome is TeamOutcome.NONE:
            return None

        if form_team.outcome in {TeamOutcome.TEAM, TeamOutcome.INDIVIDUAL}:
            response_owners = form_team.eligible_members
        else:
            response_owners = form_team.eligible_members
            if not response_owners and form_team.intent is TeamIntent.EXPLICIT_MEMBERS:
                response_owners = responder_pool

        if not response_owners:
            return None
        return min(response_owners, key=lambda x: x.full_id)

    def _team_response_action(
        self,
        form_team: TeamResolution,
        responder_pool: list[MatrixID],
    ) -> _ResponseAction | None:
        """Return the action implied by one team-formation decision, if any."""
        if form_team.outcome is TeamOutcome.NONE:
            return None
        response_owner = self._response_owner_for_team_resolution(form_team, responder_pool)
        if response_owner is None:
            return _ResponseAction(kind="skip")
        if self.matrix_id != response_owner:
            return _ResponseAction(kind="skip")
        if form_team.outcome is TeamOutcome.TEAM:
            return _ResponseAction(kind="team", form_team=form_team)
        if form_team.outcome is TeamOutcome.INDIVIDUAL:
            return _ResponseAction(kind="individual")
        assert form_team.reason is not None
        return _ResponseAction(
            kind="reject",
            form_team=form_team,
            rejection_message=form_team.reason,
        )

    async def _handle_router_dispatch(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        context: _MessageContext,
        requester_user_id: str,
        *,
        message: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> _RouterDispatchResult:
        """Run the router dispatch logic shared by text and media handlers.

        Returns whether router handling should short-circuit normal dispatch, and
        whether a display-only router voice echo should count as handled.
        """
        if self.agent_name != ROUTER_AGENT_NAME:
            return _RouterDispatchResult(handled=False)

        agents_in_thread = get_agents_in_thread(context.thread_history, self.config, self.runtime_paths)
        sender_visible = filter_agents_by_sender_permissions(
            agents_in_thread,
            requester_user_id,
            self.config,
            self.runtime_paths,
        )

        if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
            if context.is_thread and has_multiple_non_agent_users_in_thread(
                context.thread_history,
                self.config,
                self.runtime_paths,
            ):
                self.logger.info("Skipping routing: multiple non-agent users in thread (mention required)")
                return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)
            available_agents = get_available_agents_for_sender(
                room,
                requester_user_id,
                self.config,
                self.runtime_paths,
            )
            if len(available_agents) == 1:
                self.logger.info("Skipping routing: only one agent present")
                return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)
            await self._handle_ai_routing(
                room,
                event,
                context.thread_history,
                context.thread_id,
                message=message,
                requester_user_id=requester_user_id,
                extra_content=extra_content,
            )
            return _RouterDispatchResult(handled=True)
        return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)

    async def _resolve_response_action(
        self,
        context: _MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
    ) -> _ResponseAction:
        """Decide whether to respond as a team, individually, or skip.

        Shared by text and image handlers to avoid duplicating the team
        formation + should-respond decision.
        """
        agents_in_thread = get_agents_in_thread(context.thread_history, self.config, self.runtime_paths)
        available_agents_in_room = get_available_agents_for_sender(
            room,
            requester_user_id,
            self.config,
            self.runtime_paths,
        )
        materializable_agent_names = self._materializable_agent_names()
        responder_pool = self._filter_materializable_agents(
            available_agents_in_room,
            materializable_agent_names,
        )
        form_team = await self._decide_team_for_sender(
            agents_in_thread,
            context,
            room,
            requester_user_id,
            message,
            is_dm,
            available_agents_in_room=available_agents_in_room,
            materializable_agent_names=materializable_agent_names,
        )
        team_action = self._team_response_action(form_team, responder_pool)
        if team_action is not None:
            return team_action

        if not should_agent_respond(
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
        ):
            return _ResponseAction(kind="skip")

        return _ResponseAction(kind="individual")

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
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(
            context.thread_history,
            self.config,
            self.runtime_paths,
        )
        if available_agents_in_room is None:
            available_agents_in_room = get_available_agents_for_sender(
                room,
                requester_user_id,
                self.config,
                self.runtime_paths,
            )
        if materializable_agent_names is None:
            materializable_agent_names = self._materializable_agent_names()
        return await decide_team_formation(
            self.matrix_id,
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            room=room,
            message=message,
            config=self.config,
            runtime_paths=self.runtime_paths,
            is_dm_room=is_dm,
            is_thread=context.is_thread,
            available_agents_in_room=available_agents_in_room,
            materializable_agent_names=materializable_agent_names,
        )

    async def _extract_dispatch_context(self, room: nio.MatrixRoom, event: _DispatchEvent) -> _MessageContext:
        """Extract lightweight routing context without hydrating full thread history."""
        return await self._extract_message_context(room, event, full_history=False)

    async def _extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        full_history: bool = True,
    ) -> _MessageContext:
        """Extract message context, optionally using a lightweight thread snapshot."""
        assert self.client is not None
        resolved_event_source = await resolve_event_source_content(event.source, self.client)

        # Check if mentions should be ignored for this message
        skip_mentions = _should_skip_mentions(resolved_event_source)

        if skip_mentions:
            # Don't detect mentions if the message has skip_mentions metadata
            mentioned_agents: list[MatrixID] = []
            am_i_mentioned = False
            has_non_agent_mentions = False
        else:
            mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
                resolved_event_source,
                self.matrix_id,
                self.config,
                self.runtime_paths,
            )

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)

        event_info = EventInfo.from_event(resolved_event_source)
        if self.config.get_entity_thread_mode(self.agent_name, self.runtime_paths, room_id=room.room_id) == "room":
            is_thread = False
            thread_id = None
            thread_history: list[ResolvedVisibleMessage] = []
            requires_full_thread_history = False
        elif full_history:
            is_thread, thread_id, thread_history = await self._derive_conversation_context(
                room.room_id,
                event_info,
            )
            requires_full_thread_history = False
        else:
            (
                is_thread,
                thread_id,
                thread_history,
                requires_full_thread_history,
            ) = await self._derive_conversation_target(
                room.room_id,
                event_info,
            )

        return _MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            requires_full_thread_history=requires_full_thread_history,
        )

    async def _hydrate_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        context: _MessageContext,
    ) -> None:
        """Replace lightweight thread snapshots with full history once a reply is required."""
        if not context.requires_full_thread_history or context.thread_id is None:
            context.requires_full_thread_history = False
            return
        full_context = await self._extract_message_context(room, event)
        context.thread_history = full_context.thread_history
        context.is_thread = full_context.is_thread
        context.thread_id = full_context.thread_id
        context.requires_full_thread_history = False

    def _cached_room(self, room_id: str) -> nio.MatrixRoom | None:
        """Return room from client cache when available."""
        client = self.client
        if client is None:
            return None
        return client.rooms.get(room_id)

    def _build_tool_runtime_context(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        user_id: str | None,
        session_id: str | None = None,
        *,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | None = None,
        correlation_id: str | None = None,
        resolved_thread_id: str | None = None,
    ) -> ToolRuntimeContext | None:
        """Build shared runtime context for all tool calls."""
        if self.client is None:
            return None
        return ToolRuntimeContext(
            agent_name=agent_name or self.agent_name,
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=(
                resolved_thread_id
                if resolved_thread_id is not None
                else self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
            ),
            requester_id=user_id or self.matrix_id.full_id,
            client=self.client,
            config=self.config,
            runtime_paths=self.runtime_paths,
            active_model_name=active_model_name,
            session_id=session_id,
            room=self._cached_room(room_id),
            reply_to_event_id=reply_to_event_id,
            storage_path=self.storage_path,
            attachment_ids=tuple(attachment_ids or []),
            hook_registry=self.hook_registry,
            correlation_id=correlation_id,
            hook_message_sender=self._hook_message_sender(),
        )

    def _resolve_runtime_model_for_room(self, room_id: str) -> str:
        """Return the effective configured model name for this bot in one room."""
        return self.config.resolve_runtime_model(
            entity_name=self.agent_name,
            room_id=room_id,
            runtime_paths=self.runtime_paths,
        ).model_name

    def _history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.agent_name in self.config.teams:
            return HistoryScope(kind="team", scope_id=self.agent_name)
        return HistoryScope(kind="agent", scope_id=self.agent_name)

    def _history_session_type(self) -> SessionType:
        """Return the Agno session type used by this bot's persisted history."""
        return SessionType.TEAM if self.agent_name in self.config.teams else SessionType.AGENT

    def _create_history_scope_storage(
        self,
        execution_identity: ToolExecutionIdentity | None,
    ) -> SqliteDb:
        """Create the canonical storage backing this bot's persisted history scope."""
        return create_scope_session_storage(
            agent_name=self.agent_name,
            scope=self._history_scope(),
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=execution_identity,
        )

    def _team_history_scope(self, team_agents: list[MatrixID]) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        if self.agent_name in self.config.teams:
            return HistoryScope(kind="team", scope_id=self.agent_name)
        team_member_names = [
            matrix_id.agent_name(self.config, self.runtime_paths) or matrix_id.username for matrix_id in team_agents
        ]
        return HistoryScope(kind="team", scope_id=f"team_{'+'.join(sorted(team_member_names))}")

    def _strip_transient_enrichment_from_history(
        self,
        *,
        scope: HistoryScope,
        session_id: str,
        session_type: SessionType,
        execution_identity: ToolExecutionIdentity | None,
        failure_message: str,
    ) -> None:
        """Remove hook-provided transient enrichment from one persisted session."""
        try:
            with open_scope_storage(
                agent_name=self.agent_name,
                scope=scope,
                config=self.config,
                runtime_paths=self.runtime_paths,
                execution_identity=execution_identity,
            ) as storage:
                strip_enrichment_from_session_storage(
                    storage,
                    session_id,
                    session_type=session_type,
                )
        except Exception:
            self.logger.exception(
                failure_message,
                agent_name=self.agent_name,
                session_id=session_id,
            )

    def _build_tool_execution_identity(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        user_id: str | None,
        session_id: str,
        agent_name: str | None = None,
        resolved_thread_id: str | None = None,
    ) -> ToolExecutionIdentity:
        """Build the serializable execution identity used for worker routing."""
        return build_tool_execution_identity(
            channel="matrix",
            agent_name=agent_name or self.agent_name,
            runtime_paths=self.runtime_paths,
            requester_id=user_id or self.matrix_id.full_id,
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=(
                resolved_thread_id
                if resolved_thread_id is not None
                else self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
            ),
            session_id=session_id,
        )

    def _agent_has_matrix_messaging_tool(self, agent_name: str) -> bool:
        """Return whether an agent can issue Matrix message actions."""
        try:
            tool_names = self.config.get_agent_tools(agent_name)
        except ValueError:
            return False
        if not isinstance(tool_names, list | tuple | set):
            return False
        return "matrix_message" in tool_names

    def _append_matrix_prompt_context(
        self,
        prompt: str,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        include_context: bool,
        resolved_thread_id: str | None = None,
    ) -> str:
        """Append room/thread/event ids to the LLM prompt when messaging tools are available."""
        if not include_context:
            return prompt
        if self._MATRIX_PROMPT_CONTEXT_MARKER in prompt:
            return prompt

        effective_thread_id = (
            resolved_thread_id
            if resolved_thread_id is not None
            else self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
        )
        metadata_block = "\n".join(
            (
                self._MATRIX_PROMPT_CONTEXT_MARKER,
                f"room_id: {room_id}",
                f"thread_id: {effective_thread_id or 'none'}",
                f"reply_to_event_id: {reply_to_event_id or 'none'}",
                "Use these IDs when calling matrix_message.",
            ),
        )
        return f"{prompt.rstrip()}\n\n{metadata_block}"

    def _prepare_response_runtime(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        prompt: str,
        user_id: str | None,
        response_target: _ResponseTarget,
        include_context: bool,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> _PreparedResponseRuntime:
        """Derive prompt metadata and tool runtime from one canonical response target."""
        return _PreparedResponseRuntime(
            model_prompt=self._append_matrix_prompt_context(
                prompt,
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                include_context=include_context,
                resolved_thread_id=response_target.resolved_thread_id,
            ),
            tool_context=self._build_tool_runtime_context(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                user_id=user_id,
                agent_name=agent_name,
                active_model_name=active_model_name,
                session_id=response_target.session_id,
                attachment_ids=attachment_ids,
                correlation_id=correlation_id,
                resolved_thread_id=response_target.resolved_thread_id,
            ),
            execution_identity=self._build_tool_execution_identity(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                user_id=user_id,
                session_id=response_target.session_id,
                agent_name=agent_name,
                resolved_thread_id=response_target.resolved_thread_id,
            ),
        )

    def _prefix_user_turn_time(self, prompt: str, *, timestamp_ms: float | None = None) -> str:
        """Prefix a user turn with its local date and wall-clock time."""
        if not prompt.strip() or strip_user_turn_time_prefix(prompt) != prompt:
            return prompt
        tz = ZoneInfo(self.config.timezone)
        current = datetime.now(tz) if timestamp_ms is None else datetime.fromtimestamp(timestamp_ms / 1000, tz)
        timezone_abbrev = current.tzname() or self.config.timezone
        return f"[{current.strftime('%Y-%m-%d %H:%M')} {timezone_abbrev}] {prompt}"

    def _timestamp_thread_history_user_turns(
        self,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> list[ResolvedVisibleMessage]:
        """Add time prefixes to user-authored thread-history entries."""
        timestamped_history: list[ResolvedVisibleMessage] = []
        for message in thread_history:
            body = message.body
            content = message.content
            sender = message.sender
            is_user_turn = (isinstance(content, dict) and isinstance(content.get(ORIGINAL_SENDER_KEY), str)) or (
                isinstance(sender, str) and not is_agent_id(sender, self.config, self.runtime_paths)
            )
            if not isinstance(body, str) or not is_user_turn:
                timestamped_history.append(message)
                continue

            message_timestamp = message.timestamp
            timestamp_ms = message_timestamp if isinstance(message_timestamp, int | float) else None
            timestamped_body = self._prefix_user_turn_time(body, timestamp_ms=timestamp_ms)
            timestamped_history.append(replace_visible_message(message, body=timestamped_body))
        return timestamped_history

    def _timestamp_model_user_context(
        self,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> tuple[str, list[ResolvedVisibleMessage]]:
        """Return model-facing prompt/history with local timestamps added to user turns."""
        return self._prefix_user_turn_time(prompt), self._timestamp_thread_history_user_turns(thread_history)

    def _prepare_memory_and_model_context(
        self,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        *,
        model_prompt: str | None = None,
    ) -> tuple[str, Sequence[ResolvedVisibleMessage], str, list[ResolvedVisibleMessage]]:
        """Return raw memory inputs alongside timestamped model-facing context."""
        model_prompt_text, model_thread_history = self._timestamp_model_user_context(
            model_prompt or prompt,
            thread_history,
        )
        return prompt, thread_history, model_prompt_text, model_thread_history

    async def _apply_message_enrichment(
        self,
        dispatch: _PreparedDispatch,
        payload: _DispatchPayload,
        *,
        target_entity_name: str,
        target_member_names: tuple[str, ...] | None,
    ) -> _PreparedHookedPayload:
        """Run message:enrich and return the model-facing payload."""
        envelope = MessageEnvelope(
            source_event_id=dispatch.envelope.source_event_id,
            room_id=dispatch.envelope.room_id,
            thread_id=dispatch.envelope.thread_id,
            resolved_thread_id=dispatch.envelope.resolved_thread_id,
            requester_id=dispatch.envelope.requester_id,
            sender_id=dispatch.envelope.sender_id,
            body=dispatch.envelope.body,
            attachment_ids=(
                tuple(payload.attachment_ids)
                if payload.attachment_ids is not None
                else dispatch.envelope.attachment_ids
            ),
            mentioned_agents=dispatch.envelope.mentioned_agents,
            agent_name=target_entity_name,
            source_kind=dispatch.envelope.source_kind,
        )
        model_prompt: str | None = None
        strip_transient_enrichment_after_run = False
        if self.hook_registry.has_hooks(EVENT_MESSAGE_ENRICH):
            context = MessageEnrichContext(
                **self._hook_base_kwargs(EVENT_MESSAGE_ENRICH, dispatch.correlation_id),
                envelope=envelope,
                target_entity_name=target_entity_name,
                target_member_names=target_member_names,
            )
            items = await emit_collect(self.hook_registry, EVENT_MESSAGE_ENRICH, context)
            if items:
                enrichment_block = render_enrichment_block(items)
                model_prompt = f"{payload.prompt.rstrip()}\n\n{enrichment_block}"
                strip_transient_enrichment_after_run = True

        return _PreparedHookedPayload(
            payload=_DispatchPayload(
                prompt=payload.prompt,
                model_prompt=model_prompt,
                media=payload.media,
                attachment_ids=payload.attachment_ids,
            ),
            envelope=envelope,
            strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
        )

    async def _apply_before_response_hooks(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
        """Run message:before_response hooks on one generated response."""
        draft = ResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )
        if not self.hook_registry.has_hooks(EVENT_MESSAGE_BEFORE_RESPONSE):
            return draft

        context = BeforeResponseContext(
            **self._hook_base_kwargs(EVENT_MESSAGE_BEFORE_RESPONSE, correlation_id),
            draft=draft,
        )
        return await emit_transform(self.hook_registry, EVENT_MESSAGE_BEFORE_RESPONSE, context)

    async def _emit_after_response_hooks(
        self,
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_event_id: str,
        delivery_kind: Literal["sent", "edited"],
        response_kind: str,
    ) -> None:
        """Emit message:after_response after the final send or edit succeeds."""
        if not self.hook_registry.has_hooks(EVENT_MESSAGE_AFTER_RESPONSE):
            return

        context = AfterResponseContext(
            **self._hook_base_kwargs(EVENT_MESSAGE_AFTER_RESPONSE, correlation_id),
            result=ResponseResult(
                response_text=response_text,
                response_event_id=response_event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
                envelope=envelope,
            ),
        )
        await emit(self.hook_registry, EVENT_MESSAGE_AFTER_RESPONSE, context)

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
        payload: _DispatchPayload,
        response_envelope: MessageEnvelope | None = None,
        strip_transient_enrichment_after_run: bool = False,
        correlation_id: str | None = None,
        reason_prefix: str = "Team request",
        response_target: _ResponseTarget | None = None,
    ) -> str | None:
        """Generate a team response (shared between preformed teams and TeamBot).

        Returns the initial message ID if created, None otherwise.
        """
        effective_response_target = response_target or self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            response_envelope=response_envelope,
        )
        lifecycle_lock = self._response_lifecycle_lock(
            room_id,
            thread_id,
            reply_to_event_id,
            resolved_thread_id=effective_response_target.resolved_thread_id,
        )
        async with lifecycle_lock:
            return await self._generate_team_response_helper_locked(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                response_target=effective_response_target,
                team_agents=team_agents,
                team_mode=team_mode,
                thread_history=thread_history,
                requester_user_id=requester_user_id,
                existing_event_id=existing_event_id,
                existing_event_is_placeholder=existing_event_is_placeholder,
                payload=payload,
                response_envelope=response_envelope,
                strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
                correlation_id=correlation_id,
                reason_prefix=reason_prefix,
            )

    async def _generate_team_response_helper_locked(  # noqa: C901, PLR0915
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_target: _ResponseTarget,
        team_agents: list[MatrixID],
        team_mode: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        requester_user_id: str,
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        *,
        payload: _DispatchPayload,
        response_envelope: MessageEnvelope | None = None,
        strip_transient_enrichment_after_run: bool = False,
        correlation_id: str | None = None,
        reason_prefix: str = "Team request",
    ) -> str | None:
        """Generate a team response once the per-thread lifecycle lock is held."""
        assert self.client is not None
        prompt, thread_history = self._timestamp_model_user_context(
            payload.model_prompt or payload.prompt,
            thread_history,
        )
        # Team flows call Agno's team APIs directly instead of ai_response()/stream_agent_response().
        # This flag is only used to decide whether transient enrichment
        # must be scrubbed back out of persisted team session history after the response finishes.

        # Get the appropriate model for this team and room
        model_name = select_model_for_team(self.agent_name, room_id, self.config, self.runtime_paths)
        room_mode = self.config.get_entity_thread_mode(self.agent_name, self.runtime_paths, room_id=room_id) == "room"

        # Decide streaming based on presence
        use_streaming = await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=requester_user_id,
            enable_streaming=self.enable_streaming,
        )

        # Convert mode string to TeamMode enum
        mode = TeamMode.COORDINATE if team_mode == "coordinate" else TeamMode.COLLABORATE

        # Convert MatrixID list to agent names for non-streaming APIs
        agent_names = [mid.agent_name(self.config, self.runtime_paths) or mid.username for mid in team_agents]
        self.config.assert_team_agents_supported(
            [agent_name for agent_name in agent_names if agent_name != ROUTER_AGENT_NAME],
        )
        include_matrix_prompt_context = any(self._agent_has_matrix_messaging_tool(name) for name in agent_names)
        resolved_correlation_id = correlation_id or reply_to_event_id
        runtime = self._prepare_response_runtime(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            prompt=prompt,
            user_id=requester_user_id,
            response_target=response_target,
            include_context=include_matrix_prompt_context,
            active_model_name=model_name,
            attachment_ids=payload.attachment_ids,
            correlation_id=resolved_correlation_id,
        )
        model_message = runtime.model_prompt
        resolved_response_envelope = response_envelope or self._default_response_envelope(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            resolved_thread_id=response_target.resolved_thread_id,
            requester_id=requester_user_id,
            body=payload.prompt,
            attachment_ids=payload.attachment_ids,
        )
        delivery_thread_id = response_target.delivery_thread_id
        session_id = response_target.session_id
        tool_context = runtime.tool_context
        execution_identity = runtime.execution_identity
        orchestrator = self.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)
        response_run_id = str(uuid4())

        # Create async function for team response generation that takes message_id as parameter
        client = self.client
        delivery_result: _ResponseDispatchResult | None = None
        compaction_outcomes: list[CompactionOutcome] = []

        async def generate_team_response(message_id: str | None) -> None:
            nonlocal delivery_result

            def _note_attempt_run_id(current_run_id: str) -> None:
                self.stop_manager.update_run_id(message_id, current_run_id)

            if use_streaming and (not existing_event_id or existing_event_is_placeholder):
                # Show typing indicator while team generates streaming response
                async with typing_indicator(client, room_id):
                    with (
                        tool_execution_identity(execution_identity),
                        tool_runtime_context(tool_context),
                    ):
                        response_stream = team_response_stream(
                            agent_ids=team_agents,
                            message=model_message,
                            orchestrator=orchestrator,
                            execution_identity=execution_identity,
                            mode=mode,
                            thread_history=thread_history,
                            model_name=model_name,
                            media=payload.media,
                            show_tool_calls=self.show_tool_calls,
                            session_id=session_id,
                            run_id=response_run_id,
                            run_id_callback=_note_attempt_run_id,
                            user_id=requester_user_id,
                            reply_to_event_id=reply_to_event_id,
                            active_event_ids=self._active_response_event_ids(room_id),
                            response_sender_id=self.matrix_id.full_id,
                            compaction_outcomes_collector=compaction_outcomes,
                            configured_team_name=self.agent_name if self.agent_name in self.config.teams else None,
                            reason_prefix=reason_prefix,
                        )

                        event_id, accumulated = await send_streaming_response(
                            client,
                            room_id,
                            reply_to_event_id,
                            delivery_thread_id,
                            self.matrix_id.domain,
                            self.config,
                            self.runtime_paths,
                            response_stream,
                            streaming_cls=ReplacementStreamingResponse,
                            header=None,
                            show_tool_calls=self.show_tool_calls,
                            existing_event_id=message_id,
                            adopt_existing_placeholder=message_id is not None
                            and (existing_event_is_placeholder or existing_event_id is None),
                            room_mode=room_mode,
                        )
                if event_id is None:
                    delivery_result = _ResponseDispatchResult(
                        event_id=None,
                        response_text=accumulated,
                        delivery_kind=None,
                    )
                    return

                delivery_kind: Literal["sent", "edited"] = "edited" if message_id else "sent"
                draft = await self._apply_before_response_hooks(
                    correlation_id=resolved_correlation_id,
                    envelope=resolved_response_envelope,
                    response_text=accumulated,
                    response_kind="team",
                    tool_trace=None,
                    extra_content=None,
                )
                if draft.suppress:
                    delivery_result = await self._cleanup_suppressed_streamed_response(
                        room_id=room_id,
                        event_id=event_id,
                        response_text=accumulated,
                        response_kind="team",
                        response_envelope=resolved_response_envelope,
                        correlation_id=resolved_correlation_id,
                    )
                    return

                if draft.response_text != accumulated:
                    delivery_result = await self._deliver_generated_response(
                        room_id=room_id,
                        reply_to_event_id=reply_to_event_id,
                        thread_id=delivery_thread_id,
                        existing_event_id=event_id,
                        existing_event_is_placeholder=event_id is not None
                        and (existing_event_is_placeholder or existing_event_id is None),
                        response_text=draft.response_text,
                        response_kind="team",
                        response_envelope=resolved_response_envelope,
                        correlation_id=resolved_correlation_id,
                        tool_trace=None,
                        extra_content=None,
                        apply_before_hooks=False,
                    )
                else:
                    interactive_response = interactive.parse_and_format_interactive(accumulated, extract_mapping=True)
                    await self._emit_after_response_hooks(
                        correlation_id=resolved_correlation_id,
                        envelope=resolved_response_envelope,
                        response_text=interactive_response.formatted_text,
                        response_event_id=event_id,
                        delivery_kind=delivery_kind,
                        response_kind="team",
                    )
                    delivery_result = _ResponseDispatchResult(
                        event_id=event_id,
                        response_text=interactive_response.formatted_text,
                        delivery_kind=delivery_kind,
                        option_map=interactive_response.option_map,
                        options_list=interactive_response.options_list,
                    )
            else:
                # Show typing indicator while team generates non-streaming response
                try:
                    async with typing_indicator(client, room_id):
                        with (
                            tool_execution_identity(execution_identity),
                            tool_runtime_context(tool_context),
                        ):
                            response_text = await team_response(
                                agent_names=agent_names,
                                mode=mode,
                                message=model_message,
                                orchestrator=orchestrator,
                                execution_identity=execution_identity,
                                thread_history=thread_history,
                                model_name=model_name,
                                media=payload.media,
                                session_id=session_id,
                                run_id=response_run_id,
                                run_id_callback=_note_attempt_run_id,
                                user_id=requester_user_id,
                                reply_to_event_id=reply_to_event_id,
                                active_event_ids=self._active_response_event_ids(room_id),
                                response_sender_id=self.matrix_id.full_id,
                                compaction_outcomes_collector=compaction_outcomes,
                                configured_team_name=self.agent_name if self.agent_name in self.config.teams else None,
                                reason_prefix=reason_prefix,
                            )
                except asyncio.CancelledError:
                    self.logger.info("Team non-streaming response cancelled by user", message_id=message_id)
                    if message_id:
                        await self._edit_message(room_id, message_id, _CANCELLED_RESPONSE_TEXT, delivery_thread_id)
                    raise

                delivery_result = await self._deliver_generated_response(
                    room_id=room_id,
                    reply_to_event_id=reply_to_event_id,
                    thread_id=delivery_thread_id,
                    existing_event_id=message_id,
                    existing_event_is_placeholder=message_id is not None
                    and (existing_event_is_placeholder or existing_event_id is None),
                    response_text=response_text,
                    response_kind="team",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
                    tool_trace=None,
                    extra_content=None,
                )

        # Use unified handler for cancellation support
        # Always send thinking message unless we're editing an existing message
        thinking_msg = None
        if not existing_event_id:
            thinking_msg = "🤝 Team Response: Thinking..."

        tracked_event_id = await self._run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            resolved_thread_id=delivery_thread_id,
            response_function=generate_team_response,
            thinking_message=thinking_msg,
            existing_event_id=existing_event_id,
            user_id=requester_user_id,
            run_id=response_run_id,
        )
        if strip_transient_enrichment_after_run:
            self._strip_transient_enrichment_from_history(
                scope=self._team_history_scope(team_agents),
                session_id=session_id,
                session_type=SessionType.TEAM,
                execution_identity=execution_identity,
                failure_message="Failed to strip hook enrichment from team session history",
            )
        if (
            delivery_result is not None
            and delivery_result.event_id
            and delivery_result.option_map
            and delivery_result.options_list
        ):
            interactive.register_interactive_question(
                delivery_result.event_id,
                room_id,
                response_target.resolved_thread_id,
                delivery_result.option_map,
                "team",
            )
            await interactive.add_reaction_buttons(
                self.client,
                room_id,
                delivery_result.event_id,
                delivery_result.options_list,
            )

        if delivery_result is not None:
            await self._dispatch_compaction_notices(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                main_response_event_id=delivery_result.event_id,
                thread_id=thread_id,
                compaction_outcomes=compaction_outcomes,
            )

        return self._resolve_response_event_id(
            delivery_result=delivery_result,
            tracked_event_id=tracked_event_id,
            existing_event_id=existing_event_id,
        )

    async def _run_cancellable_response(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_function: object,  # Function that generates the response (takes message_id)
        resolved_thread_id: str | None = None,
        thinking_message: str | None = None,  # None means don't send thinking message
        existing_event_id: str | None = None,
        user_id: str | None = None,  # User ID for presence check
        run_id: str | None = None,
    ) -> str | None:
        """Run a response generation function with cancellation support.

        This unified handler provides:
        - Optional "Thinking..." message
        - Task cancellation via stop button (when user is online)
        - Proper cleanup on completion or cancellation

        Args:
            room_id: The room to send to
            reply_to_event_id: Event to reply to
            thread_id: Thread ID if in thread
            response_function: Async function that generates the response (takes message_id parameter)
            resolved_thread_id: Canonical thread root to reuse for placeholder sends and edits
            thinking_message: Thinking message to show (only used when existing_event_id is None)
            existing_event_id: ID of existing message to edit (for interactive questions)
            user_id: User ID for checking if they're online (for stop button decision)
            run_id: Explicit Agno run identifier used for graceful stop/cancel handling.

        Returns:
            The tracked response message ID, if any

        Note: In practice, either thinking_message or existing_event_id is provided, never both.

        """
        assert self.client is not None

        # Validate the mutual exclusivity constraint
        assert not (thinking_message and existing_event_id), (
            "thinking_message and existing_event_id are mutually exclusive"
        )

        try:
            # Count the full response lifecycle, including the initial
            # thinking-message send before a cancellable task exists.
            self.in_flight_response_count += 1

            # Send initial thinking message if not editing an existing message
            initial_message_id = None
            if thinking_message:
                assert not existing_event_id  # Redundant but makes the logic clear
                response_thread_id = (
                    resolved_thread_id
                    if resolved_thread_id is not None
                    else self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
                )
                initial_message_id = await self._send_response(
                    room_id,
                    reply_to_event_id,
                    f"{thinking_message} {IN_PROGRESS_MARKER}",
                    response_thread_id,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                )

            # Determine which message ID to use
            message_id = existing_event_id or initial_message_id

            # Create cancellable task by calling the function with the message ID
            task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))  # type: ignore[operator]

            # Always track the task so stop reactions still work when we do not
            # have a Matrix event ID yet.
            message_to_track = existing_event_id or initial_message_id
            tracked_message_id = message_to_track or f"__pending_response__:{id(task)}"
            show_stop_button = False  # Default to not showing

            self.stop_manager.set_current(
                tracked_message_id,
                room_id,
                task,
                None,
                run_id=run_id,
            )

            if message_to_track:
                # Add stop button if configured AND user is online
                # This uses the same logic as streaming to determine if user is online
                show_stop_button = self.config.defaults.show_stop_button
                if show_stop_button and user_id:
                    # Check if user is online - same logic as streaming decision
                    user_is_online = await is_user_online(self.client, user_id)
                    show_stop_button = user_is_online
                    self.logger.info(
                        "Stop button decision",
                        message_id=message_to_track,
                        user_online=user_is_online,
                        show_button=show_stop_button,
                    )

                if show_stop_button:
                    self.logger.info("Adding stop button", message_id=message_to_track)
                    await self.stop_manager.add_stop_button(self.client, room_id, message_to_track)

            try:
                await task
            except asyncio.CancelledError:
                self.logger.info("Response cancelled", message_id=message_to_track or tracked_message_id)
            except Exception as e:
                self.logger.exception("Error during response generation", error=str(e))
                raise
            finally:
                tracked = self.stop_manager.tracked_messages.get(tracked_message_id)
                button_already_removed = tracked is None or tracked.reaction_event_id is None

                self.stop_manager.clear_message(
                    tracked_message_id,
                    client=self.client,
                    remove_button=show_stop_button and not button_already_removed,
                )

            return message_id
        finally:
            self.in_flight_response_count -= 1

    async def _process_and_respond(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        *,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,
        run_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
        model_prompt: str | None = None,
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        resolved_thread_id: str | None = None,
        response_target: _ResponseTarget | None = None,
        response_kind: str = "ai",
    ) -> _ResponseDispatchResult:
        """Process a message and send a response (non-streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return _ResponseDispatchResult(event_id=existing_event_id, response_text="", delivery_kind=None)

        media_inputs = media or MediaInputs()
        effective_response_target = response_target or self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            resolved_thread_id=resolved_thread_id,
            response_envelope=response_envelope,
        )
        active_model_name = self._resolve_runtime_model_for_room(room_id)
        runtime = self._prepare_response_runtime(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            prompt=model_prompt or prompt,
            user_id=user_id,
            response_target=effective_response_target,
            include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
            active_model_name=active_model_name,
            attachment_ids=attachment_ids,
            correlation_id=correlation_id,
        )
        response_thread_id = effective_response_target.delivery_thread_id
        session_id = effective_response_target.session_id
        model_prompt = runtime.model_prompt
        tool_context = runtime.tool_context
        execution_identity = runtime.execution_identity
        request_knowledge_managers = await self._ensure_request_knowledge_managers(
            [self.agent_name],
            execution_identity,
        )
        tool_trace: list[ToolTraceEntry] = []
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self._active_response_event_ids(room_id)

        def _note_attempt_run_id(current_run_id: str) -> None:
            self.stop_manager.update_run_id(existing_event_id, current_run_id)

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                with (
                    tool_execution_identity(execution_identity),
                    tool_runtime_context(tool_context),
                ):
                    knowledge = self._knowledge_for_agent(
                        self.agent_name,
                        request_knowledge_managers=request_knowledge_managers,
                    )
                    response_text = await ai_response(
                        agent_name=self.agent_name,
                        prompt=model_prompt,
                        session_id=session_id,
                        runtime_paths=self.runtime_paths,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        run_id=run_id,
                        run_id_callback=_note_attempt_run_id,
                        media=media_inputs,
                        reply_to_event_id=reply_to_event_id,
                        active_event_ids=active_event_ids,
                        show_tool_calls=self.show_tool_calls,
                        tool_trace_collector=tool_trace,
                        run_metadata_collector=run_metadata_content,
                        execution_identity=execution_identity,
                        compaction_outcomes_collector=compaction_outcomes,
                    )
        except asyncio.CancelledError:
            # Handle cancellation - send a message showing it was stopped
            self.logger.info("Non-streaming response cancelled by user", message_id=existing_event_id)
            if existing_event_id:
                await self._edit_message(room_id, existing_event_id, _CANCELLED_RESPONSE_TEXT, response_thread_id)
            raise
        except Exception as e:
            self.logger.exception("Error in non-streaming response", error=str(e))
            raise

        response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)
        delivery = await self._deliver_generated_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=response_thread_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            response_text=response_text,
            response_kind=response_kind,
            response_envelope=response_envelope
            or self._default_response_envelope(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                resolved_thread_id=effective_response_target.resolved_thread_id,
                requester_id=user_id or self.matrix_id.full_id,
                body=prompt,
                attachment_ids=attachment_ids,
            ),
            correlation_id=correlation_id or reply_to_event_id,
            tool_trace=tool_trace if self.show_tool_calls else None,
            extra_content=response_extra_content,
        )
        if delivery.event_id is None or delivery.suppressed:
            return delivery

        if delivery.event_id and delivery.option_map and delivery.options_list:
            interactive.register_interactive_question(
                delivery.event_id,
                room_id,
                effective_response_target.resolved_thread_id or response_thread_id,
                delivery.option_map,
                self.agent_name,
            )
            await interactive.add_reaction_buttons(self.client, room_id, delivery.event_id, delivery.options_list)

        await self._dispatch_compaction_notices(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            main_response_event_id=delivery.event_id,
            thread_id=thread_id,
            compaction_outcomes=compaction_outcomes,
        )

        return delivery

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
    ) -> str | None:
        """Send a skill command response using a specific agent."""
        response_target = self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )
        lifecycle_lock = self._response_lifecycle_lock(
            room_id,
            thread_id,
            reply_to_event_id,
            resolved_thread_id=response_target.resolved_thread_id,
        )
        async with lifecycle_lock:
            return await self._send_skill_command_response_locked(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=prompt,
                agent_name=agent_name,
                user_id=user_id,
                reply_to_event=reply_to_event,
                response_target=response_target,
            )

    async def _send_skill_command_response_locked(
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
        response_target: _ResponseTarget,
    ) -> str | None:
        """Send a skill command response after acquiring the per-thread lock."""
        assert self.client is not None
        if not prompt.strip():
            return None
        memory_prompt, memory_thread_history, prompt, thread_history = self._prepare_memory_and_model_context(
            prompt,
            thread_history,
        )
        runtime = self._prepare_response_runtime(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            prompt=prompt,
            user_id=user_id,
            response_target=response_target,
            include_context=self._agent_has_matrix_messaging_tool(agent_name),
            agent_name=agent_name,
        )
        session_id = response_target.session_id
        model_prompt = runtime.model_prompt
        tool_context = runtime.tool_context
        execution_identity = runtime.execution_identity
        request_knowledge_managers = await self._ensure_request_knowledge_managers([agent_name], execution_identity)
        reprioritize_auto_flush_sessions(
            self.storage_path,
            self.config,
            self.runtime_paths,
            agent_name=agent_name,
            active_session_id=session_id,
            execution_identity=execution_identity,
        )
        show_tool_calls = self._show_tool_calls_for_agent(agent_name)
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self._active_response_event_ids(room_id)
        async with typing_indicator(self.client, room_id):
            with (
                tool_execution_identity(execution_identity),
                tool_runtime_context(tool_context),
            ):
                knowledge = self._knowledge_for_agent(
                    agent_name,
                    request_knowledge_managers=request_knowledge_managers,
                )
                response_text = await ai_response(
                    agent_name=agent_name,
                    prompt=model_prompt,
                    session_id=session_id,
                    runtime_paths=self.runtime_paths,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
                    knowledge=knowledge,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    show_tool_calls=show_tool_calls,
                    tool_trace_collector=tool_trace,
                    run_metadata_collector=run_metadata_content,
                    execution_identity=execution_identity,
                )

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(
            room_id,
            reply_to_event_id,
            response.formatted_text,
            response_target.delivery_thread_id,
            reply_to_event=reply_to_event,
            skip_mentions=True,
            tool_trace=tool_trace if show_tool_calls else None,
            extra_content=run_metadata_content or None,
        )

        if event_id and response.option_map and response.options_list:
            interactive.register_interactive_question(
                event_id,
                room_id,
                response_target.resolved_thread_id,
                response.option_map,
                agent_name,
            )
            await interactive.add_reaction_buttons(
                self.client,
                room_id,
                event_id,
                response.options_list,
            )

        try:
            mark_auto_flush_dirty_session(
                self.storage_path,
                self.config,
                self.runtime_paths,
                agent_name=agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
            )
            if self.config.get_agent_memory_backend(agent_name) == "mem0":
                create_background_task(
                    store_conversation_memory(
                        memory_prompt,
                        agent_name,
                        self.storage_path,
                        session_id,
                        self.config,
                        self.runtime_paths,
                        memory_thread_history,
                        user_id,
                        execution_identity=execution_identity,
                    ),
                    name=f"memory_save_{agent_name}_{session_id}",
                )
        except Exception:  # pragma: no cover
            self.logger.debug("Skipping memory storage due to configuration error")

        return event_id

    async def _handle_interactive_question(
        self,
        event_id: str | None,
        content: str,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str,
        agent_name: str | None = None,
    ) -> None:
        """Handle interactive question registration and reactions if present.

        Args:
            event_id: The message event ID
            content: The message content to check for interactive questions
            room_id: The Matrix room ID
            thread_id: Thread ID if in a thread
            reply_to_event_id: Event being replied to
            agent_name: Name of agent (for registration)

        """
        if not event_id or not self.client:
            return

        if interactive.should_create_interactive_question(content):
            response = interactive.parse_and_format_interactive(content, extract_mapping=True)
            if response.option_map and response.options_list:
                thread_root_for_registration = self._resolve_reply_thread_id(
                    thread_id,
                    reply_to_event_id,
                    room_id=room_id,
                )
                interactive.register_interactive_question(
                    event_id,
                    room_id,
                    thread_root_for_registration,
                    response.option_map,
                    agent_name or self.agent_name,
                )
                await interactive.add_reaction_buttons(
                    self.client,
                    room_id,
                    event_id,
                    response.options_list,
                )

    async def _deliver_generated_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        existing_event_id: str | None,
        existing_event_is_placeholder: bool = False,
        response_text: str,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        tool_trace: list[ToolTraceEntry] | None,
        extra_content: dict[str, Any] | None,
        apply_before_hooks: bool = True,
    ) -> _ResponseDispatchResult:
        """Apply before/after hooks around one final send or edit."""
        draft = (
            await self._apply_before_response_hooks(
                correlation_id=correlation_id,
                envelope=response_envelope,
                response_text=response_text,
                response_kind=response_kind,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
            if apply_before_hooks
            else ResponseDraft(
                response_text=response_text,
                response_kind=response_kind,
                tool_trace=tool_trace,
                extra_content=extra_content,
                envelope=response_envelope,
            )
        )
        if draft.suppress:
            self.logger.info(
                "Response suppressed by hook",
                response_kind=response_kind,
                source_event_id=response_envelope.source_event_id,
                correlation_id=correlation_id,
            )
            if existing_event_id is not None and existing_event_is_placeholder:
                return await self._redact_suppressed_response_event(
                    room_id=room_id,
                    event_id=existing_event_id,
                    response_text=draft.response_text,
                    reason="Suppressed placeholder response",
                )
            return _ResponseDispatchResult(
                event_id=None,
                response_text=draft.response_text,
                delivery_kind=None,
                suppressed=True,
            )

        interactive_response = interactive.parse_and_format_interactive(draft.response_text, extract_mapping=True)
        display_text = interactive_response.formatted_text
        if existing_event_id:
            edited = await self._edit_message(
                room_id,
                existing_event_id,
                display_text,
                thread_id,
                tool_trace=draft.tool_trace,
                extra_content=draft.extra_content,
            )
            event_id = existing_event_id if edited else None
            delivery_kind: Literal["sent", "edited"] | None = "edited" if edited else None
        else:
            event_id = await self._send_response(
                room_id,
                reply_to_event_id,
                display_text,
                thread_id,
                tool_trace=draft.tool_trace,
                extra_content=draft.extra_content,
            )
            delivery_kind = "sent" if event_id else None

        if event_id and delivery_kind is not None:
            await self._emit_after_response_hooks(
                correlation_id=correlation_id,
                envelope=response_envelope,
                response_text=display_text,
                response_event_id=event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
            )
        return _ResponseDispatchResult(
            event_id=event_id,
            response_text=display_text,
            delivery_kind=delivery_kind,
            suppressed=False,
            option_map=interactive_response.option_map,
            options_list=interactive_response.options_list,
        )

    async def _redact_suppressed_response_event(
        self,
        *,
        room_id: str,
        event_id: str,
        response_text: str,
        reason: str,
    ) -> _ResponseDispatchResult:
        """Redact one provisional response and report a suppressed no-final-event outcome."""
        redacted = await self._redact_message_event(
            room_id=room_id,
            event_id=event_id,
            reason=reason,
        )
        if not redacted:
            msg = f"failed to redact suppressed response {event_id}"
            raise _SuppressedPlaceholderCleanupError(msg)
        return _ResponseDispatchResult(
            event_id=None,
            response_text=response_text,
            delivery_kind=None,
            suppressed=True,
        )

    async def _cleanup_suppressed_streamed_response(
        self,
        *,
        room_id: str,
        event_id: str,
        response_text: str,
        response_kind: str,
        response_envelope: MessageEnvelope,
        correlation_id: str,
    ) -> _ResponseDispatchResult:
        """Remove one provisional streamed response after a suppressing hook runs."""
        self.logger.warning(
            "Streaming response was already delivered before a suppressing hook ran",
            response_kind=response_kind,
            source_event_id=response_envelope.source_event_id,
            correlation_id=correlation_id,
        )
        return await self._redact_suppressed_response_event(
            room_id=room_id,
            event_id=event_id,
            response_text=response_text,
            reason="Suppressed streamed response",
        )

    async def _process_and_respond_streaming(  # noqa: C901, PLR0911, PLR0915
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        *,
        adopt_existing_placeholder: bool = False,
        user_id: str | None = None,
        run_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
        model_prompt: str | None = None,
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        resolved_thread_id: str | None = None,
        response_target: _ResponseTarget | None = None,
        response_kind: str = "ai",
    ) -> _ResponseDispatchResult:
        """Process a message and send a response (streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return _ResponseDispatchResult(event_id=existing_event_id, response_text="", delivery_kind=None)

        media_inputs = media or MediaInputs()
        effective_response_target = response_target or self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=adopt_existing_placeholder,
            resolved_thread_id=resolved_thread_id,
            response_envelope=response_envelope,
        )
        room_mode = self.config.get_entity_thread_mode(self.agent_name, self.runtime_paths, room_id=room_id) == "room"
        runtime = self._prepare_response_runtime(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            prompt=model_prompt or prompt,
            user_id=user_id,
            response_target=effective_response_target,
            include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
            active_model_name=self._resolve_runtime_model_for_room(room_id),
            attachment_ids=attachment_ids,
            correlation_id=correlation_id,
        )
        response_thread_id = effective_response_target.delivery_thread_id
        session_id = effective_response_target.session_id
        model_prompt = runtime.model_prompt
        tool_context = runtime.tool_context
        execution_identity = runtime.execution_identity
        request_knowledge_managers = await self._ensure_request_knowledge_managers(
            [self.agent_name],
            execution_identity,
        )
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self._active_response_event_ids(room_id)
        tool_trace: list[ToolTraceEntry] = []

        def _note_attempt_run_id(current_run_id: str) -> None:
            self.stop_manager.update_run_id(existing_event_id, current_run_id)

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                with (
                    tool_execution_identity(execution_identity),
                    tool_runtime_context(tool_context),
                ):
                    knowledge = self._knowledge_for_agent(
                        self.agent_name,
                        request_knowledge_managers=request_knowledge_managers,
                    )
                    response_stream = stream_agent_response(
                        agent_name=self.agent_name,
                        prompt=model_prompt,
                        session_id=session_id,
                        runtime_paths=self.runtime_paths,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        run_id=run_id,
                        run_id_callback=_note_attempt_run_id,
                        media=media_inputs,
                        reply_to_event_id=reply_to_event_id,
                        active_event_ids=active_event_ids,
                        show_tool_calls=self.show_tool_calls,
                        run_metadata_collector=run_metadata_content,
                        execution_identity=execution_identity,
                        compaction_outcomes_collector=compaction_outcomes,
                    )
                    response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)

                    event_id, accumulated = await send_streaming_response(
                        self.client,
                        room_id,
                        reply_to_event_id,
                        response_thread_id,
                        self.matrix_id.domain,
                        self.config,
                        self.runtime_paths,
                        response_stream,
                        streaming_cls=StreamingResponse,
                        existing_event_id=existing_event_id,
                        adopt_existing_placeholder=adopt_existing_placeholder,
                        room_mode=room_mode,
                        show_tool_calls=self.show_tool_calls,
                        extra_content=response_extra_content,
                        tool_trace_collector=tool_trace,
                    )
        except asyncio.CancelledError:
            # send_streaming_response already preserves partial text and appends
            # a cancellation marker for the final edit.
            self.logger.info("Streaming cancelled by user", message_id=existing_event_id)
            raise
        except Exception as e:
            self.logger.exception("Error in streaming response", error=str(e))
            # Don't mark as responded if streaming failed
            return _ResponseDispatchResult(event_id=None, response_text="", delivery_kind=None)

        if event_id is None:
            return _ResponseDispatchResult(event_id=None, response_text=accumulated, delivery_kind=None)

        delivery_kind: Literal["sent", "edited"] = "edited" if existing_event_id else "sent"
        if response_envelope is None or correlation_id is None:
            interactive_response = interactive.parse_and_format_interactive(accumulated, extract_mapping=True)
            if event_id and interactive_response.option_map and interactive_response.options_list:
                interactive.register_interactive_question(
                    event_id,
                    room_id,
                    effective_response_target.resolved_thread_id or response_thread_id,
                    interactive_response.option_map,
                    self.agent_name,
                )
                await interactive.add_reaction_buttons(
                    self.client,
                    room_id,
                    event_id,
                    interactive_response.options_list,
                )
            return _ResponseDispatchResult(
                event_id=event_id,
                response_text=interactive_response.formatted_text,
                delivery_kind=delivery_kind,
                option_map=interactive_response.option_map,
                options_list=interactive_response.options_list,
            )

        draft = await self._apply_before_response_hooks(
            correlation_id=correlation_id,
            envelope=response_envelope,
            response_text=accumulated,
            response_kind=response_kind,
            tool_trace=tool_trace if self.show_tool_calls else None,
            extra_content=response_extra_content,
        )
        if draft.suppress:
            if adopt_existing_placeholder or existing_event_id is None:
                return await self._cleanup_suppressed_streamed_response(
                    room_id=room_id,
                    event_id=event_id,
                    response_text=accumulated,
                    response_kind=response_kind,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                )
            self.logger.warning(
                "Streaming response was already delivered before a suppressing hook ran",
                response_kind=response_kind,
                source_event_id=response_envelope.source_event_id,
                correlation_id=correlation_id,
            )
            return _ResponseDispatchResult(
                event_id=event_id,
                response_text=accumulated,
                delivery_kind=delivery_kind,
                suppressed=True,
            )

        needs_final_edit = (
            draft.response_text != accumulated
            or draft.tool_trace != (tool_trace if self.show_tool_calls else None)
            or draft.extra_content != response_extra_content
        )
        if needs_final_edit:
            delivery = await self._deliver_generated_response(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=response_thread_id,
                existing_event_id=event_id,
                existing_event_is_placeholder=adopt_existing_placeholder,
                response_text=draft.response_text,
                response_kind=response_kind,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
                tool_trace=draft.tool_trace,
                extra_content=draft.extra_content,
                apply_before_hooks=False,
            )
        else:
            interactive_response = interactive.parse_and_format_interactive(accumulated, extract_mapping=True)
            await self._emit_after_response_hooks(
                correlation_id=correlation_id,
                envelope=response_envelope,
                response_text=interactive_response.formatted_text,
                response_event_id=event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
            )
            delivery = _ResponseDispatchResult(
                event_id=event_id,
                response_text=interactive_response.formatted_text,
                delivery_kind=delivery_kind,
                option_map=interactive_response.option_map,
                options_list=interactive_response.options_list,
            )

        if delivery.event_id is not None and delivery.option_map and delivery.options_list:
            interactive.register_interactive_question(
                delivery.event_id,
                room_id,
                effective_response_target.resolved_thread_id or response_thread_id,
                delivery.option_map,
                self.agent_name,
            )
            await interactive.add_reaction_buttons(
                self.client,
                room_id,
                delivery.event_id,
                delivery.options_list,
            )

        await self._dispatch_compaction_notices(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            main_response_event_id=delivery.event_id,
            thread_id=thread_id,
            compaction_outcomes=compaction_outcomes,
        )

        return delivery

    def _resolve_response_event_id(
        self,
        delivery_result: _ResponseDispatchResult | None,
        tracked_event_id: str | None,
        existing_event_id: str | None,
    ) -> str | None:
        if delivery_result is not None:
            if delivery_result.event_id is not None:
                return delivery_result.event_id
            return None
        return tracked_event_id or existing_event_id

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
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
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
            response_envelope: Optional normalized inbound envelope for response hooks.
            correlation_id: Optional request correlation ID propagated to hook logging.

        Returns:
            Event ID of the response message, or None if failed

        """
        response_target = self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            response_envelope=response_envelope,
        )
        lifecycle_lock = self._response_lifecycle_lock(
            room_id,
            thread_id,
            reply_to_event_id,
            resolved_thread_id=response_target.resolved_thread_id,
        )
        async with lifecycle_lock:
            return await self._generate_response_locked(
                room_id=room_id,
                prompt=prompt,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                response_target=response_target,
                thread_history=thread_history,
                existing_event_id=existing_event_id,
                existing_event_is_placeholder=existing_event_is_placeholder,
                user_id=user_id,
                media=media,
                attachment_ids=attachment_ids,
                model_prompt=model_prompt,
                strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
            )

    async def _generate_response_locked(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_target: _ResponseTarget,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
        model_prompt: str | None = None,
        strip_transient_enrichment_after_run: bool = False,
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
    ) -> str | None:
        """Generate one agent response after acquiring the per-thread lock."""
        assert self.client is not None
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            self._prepare_memory_and_model_context(
                prompt,
                thread_history,
                model_prompt=model_prompt,
            )
        )
        media_inputs = media or MediaInputs()

        # Prepare session id for memory storage (store after sending response)
        session_id = response_target.session_id
        execution_identity = self._build_tool_execution_identity(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
            session_id=session_id,
            resolved_thread_id=response_target.resolved_thread_id,
        )
        reprioritize_auto_flush_sessions(
            self.storage_path,
            self.config,
            self.runtime_paths,
            agent_name=self.agent_name,
            active_session_id=session_id,
            execution_identity=execution_identity,
        )

        # Dynamically determine whether to use streaming based on user presence
        use_streaming = await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=user_id,
            enable_streaming=self.enable_streaming,
        )
        if (
            use_streaming
            and existing_event_id is not None
            and not existing_event_is_placeholder
            and response_envelope is not None
            and correlation_id is not None
            and self.hook_registry.has_hooks(EVENT_MESSAGE_BEFORE_RESPONSE)
        ):
            use_streaming = False
        delivery_result: _ResponseDispatchResult | None = None
        response_run_id = str(uuid4())
        delivery_thread_id = response_target.delivery_thread_id

        # Create async function for generation that takes message_id as parameter
        async def generate(message_id: str | None) -> None:
            nonlocal delivery_result
            if use_streaming:
                delivery_result = await self._process_and_respond_streaming(
                    room_id,
                    memory_prompt,
                    reply_to_event_id,
                    thread_id,
                    model_thread_history,
                    message_id,  # Edit the thinking message or existing
                    adopt_existing_placeholder=message_id is not None
                    and (existing_event_is_placeholder or existing_event_id is None),
                    user_id=user_id,
                    run_id=response_run_id,
                    media=media_inputs,
                    attachment_ids=attachment_ids,
                    model_prompt=model_prompt_text,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                    response_target=response_target,
                )
            else:
                delivery_result = await self._process_and_respond(
                    room_id,
                    memory_prompt,
                    reply_to_event_id,
                    thread_id,
                    model_thread_history,
                    message_id,  # Edit the thinking message or existing
                    existing_event_is_placeholder=message_id is not None
                    and (existing_event_is_placeholder or existing_event_id is None),
                    user_id=user_id,
                    run_id=response_run_id,
                    media=media_inputs,
                    attachment_ids=attachment_ids,
                    model_prompt=model_prompt_text,
                    response_envelope=response_envelope,
                    correlation_id=correlation_id,
                    response_target=response_target,
                )

        # Use unified handler for cancellation support
        # Always send "Thinking..." message unless we're editing an existing message
        thinking_msg = None
        if not existing_event_id:
            thinking_msg = "Thinking..."

        tracked_event_id = await self._run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            resolved_thread_id=delivery_thread_id,
            response_function=generate,
            thinking_message=thinking_msg,
            existing_event_id=existing_event_id,
            user_id=user_id,
            run_id=response_run_id,
        )

        if strip_transient_enrichment_after_run:
            self._strip_transient_enrichment_from_history(
                scope=self._history_scope(),
                session_id=session_id,
                session_type=self._history_session_type(),
                execution_identity=execution_identity,
                failure_message="Failed to strip hook enrichment from session history",
            )

        # Store memory after response generation.
        try:
            mark_auto_flush_dirty_session(
                self.storage_path,
                self.config,
                self.runtime_paths,
                agent_name=self.agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
            )
            if self.config.get_agent_memory_backend(self.agent_name) == "mem0":
                create_background_task(
                    store_conversation_memory(
                        memory_prompt,
                        self.agent_name,
                        self.storage_path,
                        session_id,
                        self.config,
                        self.runtime_paths,
                        memory_thread_history,
                        user_id,
                        execution_identity=execution_identity,
                    ),
                    name=f"memory_save_{self.agent_name}_{session_id}",
                )
        except Exception:
            self.logger.exception(
                "Failed to queue memory persistence after response",
                agent_name=self.agent_name,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            )

        resolved_event_id = self._resolve_response_event_id(
            delivery_result=delivery_result,
            tracked_event_id=tracked_event_id,
            existing_event_id=existing_event_id,
        )

        if (
            thread_id is not None
            and resolved_event_id is not None
            and not (delivery_result is not None and delivery_result.suppressed)
        ):
            create_background_task(
                maybe_generate_thread_summary(
                    client=self.client,
                    room_id=room_id,
                    thread_id=thread_id,
                    config=self.config,
                    runtime_paths=self.runtime_paths,
                ),
                name=f"thread_summary_{room_id}_{thread_id}",
            )

        return resolved_event_id

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
    ) -> str | None:
        """Send a response message to a room.

        Args:
            room_id: The room id to send to
            reply_to_event_id: The event ID to reply to (can be None when in a thread)
            response_text: The text to send
            thread_id: The thread ID if already in a thread
            reply_to_event: Optional event object for the message we're replying to (used to check for safe thread root)
            skip_mentions: If True, add metadata to indicate mentions should not trigger responses
            tool_trace: Optional structured tool trace metadata for message content
            extra_content: Optional content fields merged into the outgoing Matrix event
            thread_mode_override: Optional thread mode to enforce for this reply

        Returns:
            Event ID if message was sent successfully, None otherwise.

        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        effective_thread_id = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
            event_source=reply_to_event.source if reply_to_event else None,
            thread_mode_override=thread_mode_override,
        )

        if effective_thread_id is None:
            # Room mode: plain message, no thread metadata
            content = format_message_with_mentions(
                self.config,
                self.runtime_paths,
                response_text,
                sender_domain=sender_domain,
                thread_event_id=None,
                reply_to_event_id=None,
                latest_thread_event_id=None,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        else:
            # Get the latest message in thread for MSC3440 fallback compatibility
            latest_thread_event_id = await get_latest_thread_event_id_if_needed(
                self.client,
                room_id,
                effective_thread_id,
                reply_to_event_id,
            )

            content = format_message_with_mentions(
                self.config,
                self.runtime_paths,
                response_text,
                sender_domain=sender_domain,
                thread_event_id=effective_thread_id,
                reply_to_event_id=reply_to_event_id,
                latest_thread_event_id=latest_thread_event_id,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )

        # Add metadata to indicate mentions should be ignored for responses
        if skip_mentions:
            content["com.mindroom.skip_mentions"] = True

        assert self.client is not None
        event_id = await send_message(self.client, room_id, content)
        if event_id:
            self.logger.info("Sent response", event_id=event_id, room_id=room_id)
            return event_id
        self.logger.error("Failed to send response to room", room_id=room_id)
        return None

    async def _dispatch_compaction_notices(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        main_response_event_id: str | None,
        thread_id: str | None,
        compaction_outcomes: list[CompactionOutcome],
    ) -> None:
        """Send compaction notices for all outcomes that have notify=True."""
        if main_response_event_id is None:
            return
        for outcome in compaction_outcomes:
            if outcome.notify:
                await self._send_compaction_notice(
                    room_id=room_id,
                    reply_to_event_id=reply_to_event_id,
                    main_response_event_id=main_response_event_id,
                    thread_id=thread_id,
                    outcome=outcome,
                )

    async def _send_compaction_notice(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        main_response_event_id: str,
        thread_id: str | None,
        outcome: CompactionOutcome,
    ) -> str | None:
        """Send a compaction notice without mention parsing side effects."""
        if self.client is None:
            return None

        summary_line = outcome.format_notice()
        formatted_body = f"<em>{html_escape(summary_line)}</em>"
        effective_thread_id = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
        )
        content = build_message_content(
            summary_line,
            formatted_body=formatted_body,
            thread_event_id=effective_thread_id,
            reply_to_event_id=main_response_event_id,
            extra_content={
                "msgtype": "m.notice",
                constants.COMPACTION_NOTICE_CONTENT_KEY: outcome.to_notice_metadata(),
                "com.mindroom.skip_mentions": True,
            },
        )
        event_id = await send_message(self.client, room_id, content)
        if event_id:
            self.logger.info(
                "Sent compaction notice",
                event_id=event_id,
                room_id=room_id,
                summary_model=outcome.summary_model,
            )
            return event_id
        self.logger.error("Failed to send compaction notice", room_id=room_id)
        return None

    async def _hook_send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None = None,
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
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        if self.config.get_entity_thread_mode(self.agent_name, self.runtime_paths, room_id=room_id) == "room":
            # Room mode: no thread metadata on edits
            content = format_message_with_mentions(
                self.config,
                self.runtime_paths,
                new_text,
                sender_domain=sender_domain,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        else:
            assert self.client is not None
            content = await build_threaded_edit_content(
                self.client,
                room_id=room_id,
                new_text=new_text,
                thread_id=thread_id,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_domain=sender_domain,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )

        assert self.client is not None
        response_event_id = await edit_message(self.client, room_id, event_id, content, new_text)

        if isinstance(response_event_id, str):
            self.logger.info("Edited message", event_id=event_id, edit_event_id=response_event_id)
            return True
        self.logger.error("Failed to edit message", event_id=event_id, error=str(response_event_id))
        return False

    async def _redact_message_event(
        self,
        *,
        room_id: str,
        event_id: str,
        reason: str,
    ) -> bool:
        """Redact one visible event when a placeholder should disappear entirely."""
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
    ) -> None:
        # Only router agent should handle routing
        assert self.agent_name == ROUTER_AGENT_NAME

        # Use configured agents only - router should not suggest random agents
        permission_sender_id = requester_user_id
        available_agents = get_configured_agents_for_room(room.room_id, self.config, self.runtime_paths)
        available_agents = filter_agents_by_sender_permissions(
            available_agents,
            permission_sender_id,
            self.config,
            self.runtime_paths,
        )
        if not available_agents:
            self.logger.debug("No configured agents to route to in this room for sender", sender=permission_sender_id)
            return

        self.logger.info("Handling AI routing", event_id=event.event_id)

        routing_text = message or event.body
        suggested_agent = await suggest_agent_for_message(
            routing_text,
            available_agents,
            self.config,
            self.runtime_paths,
            thread_history,
        )

        if not suggested_agent:
            # Send error message when routing fails
            response_text = "⚠️ I couldn't determine which agent should help with this. Please try mentioning an agent directly with @ or rephrase your request."
            self.logger.warning("Router failed to determine agent")
        else:
            # Router mentions the suggested agent and asks them to help
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = (
            self.config.get_entity_thread_mode(suggested_agent, self.runtime_paths, room_id=room.room_id)
            if suggested_agent
            else None
        )
        thread_event_id = self._resolve_reply_thread_id(
            thread_id,
            event.event_id,
            room_id=room.room_id,
            event_source=event.source,
            thread_mode_override=target_thread_mode,
        )
        routed_extra_content = dict(extra_content) if extra_content is not None else {}
        if isinstance(
            event,
            nio.RoomMessageFile
            | nio.RoomEncryptedFile
            | nio.RoomMessageVideo
            | nio.RoomEncryptedVideo
            | nio.RoomMessageImage
            | nio.RoomEncryptedImage,
        ):
            attachment_id = await self._register_routed_attachment(
                room_id=room.room_id,
                thread_id=thread_event_id,
                event=event,
            )
            if attachment_id is None:
                routed_extra_content.pop(ATTACHMENT_IDS_KEY, None)
            else:
                routed_extra_content[ATTACHMENT_IDS_KEY] = [attachment_id]

        event_id = await self._send_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=response_text,
            thread_id=thread_event_id,
            extra_content=routed_extra_content or None,
            thread_mode_override=target_thread_mode,
        )
        if event_id:
            self.logger.info("Routed to agent", suggested_agent=suggested_agent)
            self.response_tracker.mark_responded(event.event_id)
        else:
            self.logger.error("Failed to route to agent", agent=suggested_agent)

    async def _handle_message_edit(
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

        # Skip edits from other agents
        sender_agent_name = extract_agent_name(event.sender, self.config, self.runtime_paths)
        if sender_agent_name:
            self.logger.debug(f"Ignoring edit from other agent: {sender_agent_name}")
            return

        response_event_id = self.response_tracker.get_response_event_id(event_info.original_event_id)
        if not response_event_id:
            self.logger.debug(f"No previous response found for edited message {event_info.original_event_id}")
            return

        self.logger.info(
            "Regenerating response for edited message",
            original_event_id=event_info.original_event_id,
            response_event_id=response_event_id,
        )

        context = await self._extract_message_context(room, event)
        edited_content, _ = await extract_edit_body(event.source, self.client)
        if edited_content is None:
            self.logger.debug("Edited message missing resolved body", event_id=event.event_id)
            return
        envelope = self._build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            body=edited_content,
            source_kind="edit",
        )
        if await self._emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
        ):
            self.response_tracker.mark_responded(event_info.original_event_id, response_event_id)
            return

        # Check if we should respond to the edited message
        # KNOWN LIMITATION: This doesn't work correctly for the router suggestion case.
        # When: User asks question → Router suggests agent → Agent responds → User edits
        # The agent won't regenerate because it's not mentioned in the edited message.
        # Proper fix would require tracking response chains (user → router → agent).
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

        if not should_respond:
            self.logger.debug("Agent should not respond to edited message")
            return

        self._remove_stale_runs_for_edited_message(
            room=room,
            thread_id=context.thread_id,
            original_event_id=event_info.original_event_id,
            requester_user_id=requester_user_id,
        )

        # Generate new response
        regenerated_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=edited_content,
            reply_to_event_id=event_info.original_event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            existing_event_is_placeholder=False,
            user_id=requester_user_id,
            response_envelope=envelope,
            correlation_id=event.event_id,
        )

        # Update the response tracker
        if regenerated_event_id is not None:
            self.response_tracker.mark_responded(event_info.original_event_id, regenerated_event_id)
            self.logger.info("Successfully regenerated response for edited message")
        else:
            self.logger.info(
                "Suppressed regeneration left existing response unchanged",
                original_event_id=event_info.original_event_id,
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
        resolved_thread_id = self._resolved_conversation_thread_id(
            room_id=room.room_id,
            thread_id=thread_id,
            reply_to_event_id=original_event_id,
        )
        session_contexts = [
            (
                resolved_thread_id,
                create_session_id(room.room_id, resolved_thread_id),
            ),
            (None, create_session_id(room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        for candidate_thread_id, session_id in session_contexts:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            execution_identity = self._build_tool_execution_identity(
                room_id=room.room_id,
                thread_id=candidate_thread_id,
                reply_to_event_id=original_event_id,
                user_id=requester_user_id,
                session_id=session_id,
            )
            storage = self._create_history_scope_storage(execution_identity)
            try:
                removed = remove_run_by_event_id(
                    storage,
                    session_id,
                    original_event_id,
                    session_type=self._history_session_type(),
                )
            finally:
                storage.close()
            if removed:
                self.logger.info(
                    "Removed stale run for edited message",
                    event_id=original_event_id,
                    session_id=session_id,
                )

    async def _handle_command(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedTextDispatchEvent,
        command: Command,
    ) -> None:
        assert self.client is not None
        event = await self._resolve_text_dispatch_event(prechecked_event.event)
        context = CommandHandlerContext(
            client=self.client,
            config=self.config,
            runtime_paths=self.runtime_paths,
            storage_path=self.storage_path,
            logger=self.logger,
            response_tracker=self.response_tracker,
            derive_conversation_context=self._derive_conversation_context,
            resolve_reply_thread_id=self._resolve_reply_thread_id,
            send_response=self._send_response,
            send_skill_command_response=self._send_skill_command_response,
        )
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id=prechecked_event.requester_user_id,
        )


@dataclass
class TeamBot(AgentBot):
    """A bot that represents a team of agents working together."""

    team_agents: list[MatrixID] = field(default_factory=list)
    team_mode: str = field(default="coordinate")
    team_model: str | None = field(default=None)

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
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
    ) -> str | None:
        """Generate a team response instead of individual agent response."""
        if not prompt.strip():
            return None

        assert self.client is not None
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            self._prepare_memory_and_model_context(
                prompt,
                thread_history,
                model_prompt=model_prompt,
            )
        )

        configured_mode = TeamMode.COORDINATE if self.team_mode == "coordinate" else TeamMode.COLLABORATE
        materializable_agent_names = self._materializable_agent_names()
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

        response_target = self._prepare_response_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
            existing_event_is_placeholder=existing_event_is_placeholder,
            response_envelope=response_envelope,
        )

        # Store memory once for the entire team (avoids duplicate LLM processing)
        session_id = response_target.session_id
        execution_identity = self._build_tool_execution_identity(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
            session_id=session_id,
            resolved_thread_id=response_target.resolved_thread_id,
        )
        # Convert MatrixID list to agent names for memory storage
        agent_names = [
            mid.agent_name(self.config, self.runtime_paths) or mid.username for mid in team_resolution.eligible_members
        ]
        with tool_execution_identity(execution_identity):
            create_background_task(
                store_conversation_memory(
                    memory_prompt,
                    agent_names,  # Pass list of agent names for team storage
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
        self.logger.info(f"Storing memory for team: {agent_names}")

        media_inputs = media or MediaInputs()

        # Use the shared team response helper
        event_id = await self._generate_team_response_helper(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            payload=_DispatchPayload(
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
            or self._default_response_envelope(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                resolved_thread_id=response_target.resolved_thread_id,
                requester_id=user_id or self.matrix_id.full_id,
                body=memory_prompt,
                attachment_ids=attachment_ids,
            ),
            strip_transient_enrichment_after_run=strip_transient_enrichment_after_run,
            correlation_id=correlation_id or reply_to_event_id,
            reason_prefix=f"Team '{self.agent_name}'",
            response_target=response_target,
        )

        if thread_id is not None and event_id is not None:
            create_background_task(
                maybe_generate_thread_summary(
                    client=self.client,
                    room_id=room_id,
                    thread_id=thread_id,
                    config=self.config,
                    runtime_paths=self.runtime_paths,
                ),
                name=f"thread_summary_{room_id}_{thread_id}",
            )

        return event_id
