"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import nio
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.matrix import image_handler
from mindroom.matrix.avatar import check_and_set_avatar
from mindroom.matrix.client import (
    PermanentMatrixStartupError,
    _latest_thread_event_id,
    edit_message,
    fetch_thread_history,
    get_joined_rooms,
    get_latest_thread_event_id_if_needed,
    join_room,
    send_message,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name, is_agent_id
from mindroom.matrix.media import extract_media_caption
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.presence import (
    build_agent_status_message,
    is_user_online,
    set_presence_status,
    should_use_streaming,
)
from mindroom.matrix.reply_chain import ReplyChainCaches, derive_conversation_context
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.rooms import is_dm_room, leave_non_dm_rooms, resolve_room_aliases
from mindroom.matrix.state import MatrixState
from mindroom.matrix.typing import typing_indicator
from mindroom.matrix.users import AgentMatrixUser, create_agent_user, login_agent_user
from mindroom.memory import store_conversation_memory
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
from mindroom.teams import (
    TeamFormationDecision,
    TeamMode,
    decide_team_formation,
    select_model_for_team,
    team_response,
    team_response_stream,
)
from mindroom.thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_configured_agents_for_room,
    has_multiple_non_agent_users_in_thread,
    has_user_responded_after_message,
    should_agent_respond,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

from . import interactive, voice_handler
from .agents import create_agent, create_session_storage, remove_run_by_event_id
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
from .bot_runtime.types import (
    _DispatchEvent,
    _DispatchPayload,
    _MediaDispatchEvent,
    _merge_response_extra_content,
    _MessageContext,
    _PreparedDispatch,
    _ResponseAction,
    _RouterDispatchResult,
    _SyntheticTextEvent,
    _TextDispatchEvent,
)
from .commands import config_confirmation
from .commands.handler import CommandHandlerContext, _generate_welcome_message, handle_command
from .commands.parsing import Command, command_parser
from .constants import (
    ATTACHMENT_IDS_KEY,
    MATRIX_HOMESERVER,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from .knowledge.utils import MultiKnowledgeVectorDb, resolve_agent_knowledge
from .logging_config import emoji, get_logger
from .media_inputs import MediaInputs
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .scheduling import (
    cancel_all_running_scheduled_tasks,
    restore_scheduled_tasks,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import structlog
    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.media import Image

    from mindroom.commands.handler import CommandEvent
    from mindroom.config.main import Config
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

_SYNC_TIMEOUT_MS = 30000

__all__ = [
    "AgentBot",
    "MultiKnowledgeVectorDb",
    "TeamBot",
    "_DispatchPayload",
    "_MessageContext",
    "_create_task_wrapper",
    "_should_skip_mentions",
    "interactive",
    "voice_handler",
]


def _create_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    schedule_background_task: Callable[[Coroutine[object, object, object]], asyncio.Task[object]] = (
        create_background_task
    ),
) -> Callable[..., Awaitable[None]]:
    """Create a wrapper that runs the callback as a background task."""

    async def wrapper(*args: object, **kwargs: object) -> None:
        async def error_handler() -> None:
            try:
                await callback(*args, **kwargs)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error in event callback")

        schedule_background_task(error_handler())

    return wrapper


def _should_skip_mentions(event_source: dict[str, Any]) -> bool:
    """Check if mentions in this message should be ignored for agent responses."""
    content = event_source.get("content", {})
    return bool(content.get("com.mindroom.skip_mentions", False))


def create_bot_for_entity(
    entity_name: str,
    agent_user: AgentMatrixUser,
    config: Config,
    storage_path: Path,
) -> AgentBot | TeamBot:
    """Create the appropriate bot instance for an entity."""
    enable_streaming = config.defaults.enable_streaming

    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        team_matrix_ids = [MatrixID.from_agent(agent_name, config.domain) for agent_name in team_config.agents]
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            rooms=rooms,
            team_agents=team_matrix_ids,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=enable_streaming,
        )

    if entity_name in config.agents:
        agent_config = config.agents[entity_name]
        rooms = resolve_room_aliases(agent_config.rooms)
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    msg = f"Entity '{entity_name}' not found in configuration."
    raise ValueError(msg)


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    _MATRIX_PROMPT_CONTEXT_MARKER = "[Matrix metadata for tool calls]"

    agent_user: AgentMatrixUser
    storage_path: Path
    config: Config
    rooms: list[str] = field(default_factory=list)

    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    enable_streaming: bool = field(default=True)
    orchestrator: MultiAgentOrchestrator | None = field(default=None, init=False)
    _reply_chain: ReplyChainCaches = field(default_factory=ReplyChainCaches, init=False)

    @property
    def agent_name(self) -> str:
        """Get the agent name from the backing user account."""
        return self.agent_user.agent_name

    @cached_property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Get a logger with agent context bound."""
        return logger.bind(agent=emoji(self.agent_name))

    @cached_property
    def matrix_id(self) -> MatrixID:
        """Get the Matrix ID for this agent bot."""
        return self.agent_user.matrix_id

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

    def _get_shared_knowledge(self, base_id: str) -> Knowledge | None:
        """Get shared knowledge instance for a configured knowledge base."""
        orchestrator = self.orchestrator
        if orchestrator is None:
            return None
        manager = orchestrator.knowledge_managers.get(base_id)
        if manager is None:
            return None
        return manager.get_knowledge()

    def _knowledge_for_agent(self, agent_name: str) -> Knowledge | None:
        """Return shared knowledge for agents assigned to one or more knowledge bases."""
        return resolve_agent_knowledge(
            agent_name,
            self.config,
            self._get_shared_knowledge,
            on_missing_bases=lambda missing_base_ids: self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            ),
        )

    @property
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        knowledge = self._knowledge_for_agent(self.agent_name)
        return create_agent(
            agent_name=self.agent_name,
            config=self.config,
            storage_path=self.storage_path,
            knowledge=knowledge,
        )

    @cached_property
    def response_tracker(self) -> ResponseTracker:
        """Get or create the response tracker for this agent."""
        tracking_dir = self.storage_path / "tracking"
        return ResponseTracker(self.agent_name, base_path=tracking_dir)

    @cached_property
    def stop_manager(self) -> StopManager:
        """Get or create the StopManager for this agent."""
        return StopManager()

    def _resolve_reply_thread_id(
        self,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        *,
        room_id: str | None = None,
        event_source: dict[str, Any] | None = None,
        thread_mode_override: Literal["thread", "room"] | None = None,
    ) -> str | None:
        """Resolve the effective thread root for outgoing replies."""
        effective_thread_mode = thread_mode_override or self.config.get_entity_thread_mode(
            self.agent_name,
            room_id=room_id,
        )
        if effective_thread_mode == "room":
            return None
        event_info = EventInfo.from_event(event_source)
        return thread_id or event_info.safe_thread_root or reply_to_event_id

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
        """Send a response message to a room."""
        sender_domain = self.matrix_id.domain
        effective_thread_id = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
            event_source=reply_to_event.source if reply_to_event else None,
            thread_mode_override=thread_mode_override,
        )

        if effective_thread_id is None:
            content = format_message_with_mentions(
                self.config,
                response_text,
                sender_domain=sender_domain,
                thread_event_id=None,
                reply_to_event_id=None,
                latest_thread_event_id=None,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        else:
            latest_thread_event_id = await get_latest_thread_event_id_if_needed(
                self.client,
                room_id,
                effective_thread_id,
                reply_to_event_id,
            )
            content = format_message_with_mentions(
                self.config,
                response_text,
                sender_domain=sender_domain,
                thread_event_id=effective_thread_id,
                reply_to_event_id=reply_to_event_id,
                latest_thread_event_id=latest_thread_event_id,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )

        if skip_mentions:
            content["com.mindroom.skip_mentions"] = True

        assert self.client is not None
        event_id = await send_message(self.client, room_id, content)
        if event_id:
            self.logger.info("Sent response", event_id=event_id, room_id=room_id)
            return event_id
        self.logger.error("Failed to send response to room", room_id=room_id)
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
        """Edit an existing message."""
        sender_domain = self.matrix_id.domain

        if self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room":
            content = format_message_with_mentions(
                self.config,
                new_text,
                sender_domain=sender_domain,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        else:
            latest_thread_event_id = None
            if thread_id:
                assert self.client is not None
                latest_thread_event_id = await _latest_thread_event_id(self.client, room_id, thread_id)
                if latest_thread_event_id is None:
                    latest_thread_event_id = event_id

            content = format_message_with_mentions(
                self.config,
                new_text,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
                latest_thread_event_id=latest_thread_event_id,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )

        assert self.client is not None
        response = await edit_message(self.client, room_id, event_id, content, new_text)

        if isinstance(response, nio.RoomSendResponse):
            self.logger.info("Edited message", event_id=event_id)
            return True
        self.logger.error("Failed to edit message", event_id=event_id, error=str(response))
        return False

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

        restored_tasks = await restore_scheduled_tasks(self.client, room_id, self.config)
        if restored_tasks > 0:
            self.logger.info(f"Restored {restored_tasks} scheduled tasks in room {room_id}")

        restored_configs = await config_confirmation.restore_pending_changes(self.client, room_id)
        if restored_configs > 0:
            self.logger.info(f"Restored {restored_configs} pending config changes in room {room_id}")

        await self._send_welcome_message_if_empty(room_id)

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for."""
        assert self.client is not None

        joined_rooms = await get_joined_rooms(self.client)
        if joined_rooms is None:
            return

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.rooms)
        if self.agent_name == ROUTER_AGENT_NAME:
            root_space_id = MatrixState.load().space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        await leave_non_dm_rooms(self.client, list(current_rooms - configured_rooms))

    async def ensure_user_account(self) -> None:
        """Ensure this agent has a Matrix user account."""
        if self.agent_user.user_id:
            return
        self.agent_user = await create_agent_user(
            MATRIX_HOMESERVER,
            self.agent_name,
            self.agent_user.display_name,
        )
        self.logger.info(f"Ensured Matrix user account: {self.agent_user.user_id}")

    async def _set_avatar_if_available(self) -> None:
        """Set avatar for the agent if an avatar file exists."""
        if not self.client:
            return

        entity_type = "teams" if self.agent_name in self.config.teams else "agents"
        avatar_path = Path(__file__).resolve().parents[2] / "avatars" / entity_type / f"{self.agent_name}.png"

        if avatar_path.exists():
            try:
                success = await check_and_set_avatar(self.client, avatar_path)
                if success:
                    self.logger.info(f"Successfully set avatar for {self.agent_name}")
                else:
                    self.logger.warning(f"Failed to set avatar for {self.agent_name}")
            except Exception as exc:
                self.logger.warning(f"Failed to set avatar: {exc}")

    async def _set_presence_with_model_info(self) -> None:
        """Set presence status with model information."""
        if self.client is None:
            return

        status_msg = build_agent_status_message(self.agent_name, self.config)
        await set_presence_status(self.client, status_msg)

    async def ensure_rooms(self) -> None:
        """Ensure the agent is in the correct rooms based on configuration."""
        await self.join_configured_rooms()
        await self.leave_unconfigured_rooms()

    async def start(self) -> None:
        """Start the agent bot with user account setup but defer room joins."""
        await self.ensure_user_account()
        self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)
        await self._set_avatar_if_available()
        await self._set_presence_with_model_info()

        invite_event_filter = cast("type[nio.Event]", nio.InviteEvent)
        text_event_filter = cast("type[nio.Event]", nio.RoomMessageText)
        reaction_event_filter = cast("type[nio.Event]", nio.ReactionEvent)
        media_event_filters = (
            cast("type[nio.Event]", nio.RoomMessageImage),
            cast("type[nio.Event]", nio.RoomEncryptedImage),
            cast("type[nio.Event]", nio.RoomMessageFile),
            cast("type[nio.Event]", nio.RoomEncryptedFile),
            cast("type[nio.Event]", nio.RoomMessageVideo),
            cast("type[nio.Event]", nio.RoomEncryptedVideo),
            cast("type[nio.Event]", nio.RoomMessageAudio),
            cast("type[nio.Event]", nio.RoomEncryptedAudio),
        )

        self.client.add_event_callback(_create_task_wrapper(self._on_invite), invite_event_filter)
        self.client.add_event_callback(_create_task_wrapper(self._on_message), text_event_filter)
        self.client.add_event_callback(_create_task_wrapper(self._on_reaction), reaction_event_filter)
        for event_filter in media_event_filters:
            self.client.add_event_callback(_create_task_wrapper(self._on_media_message), event_filter)

        self.running = True

        if self.agent_name == ROUTER_AGENT_NAME:
            try:
                await cleanup_all_orphaned_bots(self.client, self.config)
            except Exception as exc:
                self.logger.warning(f"Could not cleanup orphaned bots (non-critical): {exc}")

        self.logger.info(f"Agent setup complete: {self.agent_user.user_id}")

    async def try_start(self) -> bool:
        """Try to start the agent bot with retry logic for transient failures."""

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
        except Exception as exc:
            logger.exception(f"Failed to start agent {self.agent_name}")
            if isinstance(exc, PermanentMatrixStartupError):
                raise
            return False
        else:
            return True

    async def cleanup(self) -> None:
        """Clean up the agent by leaving all rooms and stopping."""
        assert self.client is not None
        try:
            joined_rooms = await get_joined_rooms(self.client)
            if joined_rooms:
                await leave_non_dm_rooms(self.client, joined_rooms)
        except Exception:
            self.logger.exception("Error leaving rooms during cleanup")

        await self.stop()

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False

        try:
            await wait_for_background_tasks(timeout=5.0)
            self.logger.info("Background tasks completed")
        except Exception as exc:
            self.logger.warning(f"Some background tasks did not complete: {exc}")

        if self.agent_name == ROUTER_AGENT_NAME:
            cancelled_tasks = await cancel_all_running_scheduled_tasks()
            if cancelled_tasks > 0:
                self.logger.info("Cancelled running scheduled tasks", count=cancelled_tasks)

        if self.client is not None:
            self.logger.warning("Client is not None in stop()")
            await self.client.close()
        self.logger.info("Stopped agent bot")

    async def _send_welcome_message_if_empty(self, room_id: str) -> None:
        """Send a welcome message if the room has no messages yet."""
        assert self.client is not None

        response = await self.client.room_messages(
            room_id,
            limit=2,
            message_filter={"types": ["m.room.message"]},
        )

        if not isinstance(response, nio.RoomMessagesResponse):
            self.logger.error("Failed to check room messages", room_id=room_id, error=str(response))
            return

        if not response.chunk:
            self.logger.info("Room is empty, sending welcome message", room_id=room_id)
            welcome_msg = _generate_welcome_message(room_id, self.config)
            await self._send_response(
                room_id=room_id,
                reply_to_event_id=None,
                response_text=welcome_msg,
                thread_id=None,
                skip_mentions=True,
            )
            self.logger.info("Welcome message sent", room_id=room_id)
        elif len(response.chunk) == 1:
            msg = response.chunk[0]
            if (
                isinstance(msg, nio.RoomMessageText)
                and msg.sender == self.agent_user.user_id
                and "Welcome to MindRoom" in msg.body
            ):
                self.logger.debug("Welcome message already sent", room_id=room_id)

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        await self.client.sync_forever(timeout=_SYNC_TIMEOUT_MS, full_state=True)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle room invites for the bot account."""
        assert self.client is not None
        self.logger.info("Received invite", room_id=room.room_id, sender=event.sender)
        if await join_room(self.client, room.room_id):
            self.logger.info("Joined room", room_id=room.room_id)
            if self.agent_name == ROUTER_AGENT_NAME:
                await self._send_welcome_message_if_empty(room.room_id)
        else:
            self.logger.error("Failed to join room", room_id=room.room_id)

    async def _derive_conversation_context(
        self,
        room_id: str,
        event_info: EventInfo,
    ) -> tuple[bool, str | None, list[dict[str, Any]]]:
        """Derive conversation context from threads or reply chains."""
        assert self.client is not None
        return await derive_conversation_context(
            self.client,
            room_id,
            event_info,
            self._reply_chain,
            self.logger,
            fetch_thread_history,
        )

    def _requester_user_id_for_event(self, event: CommandEvent) -> str:
        """Return the effective requester for per-user reply checks."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if (
            event.sender == self.matrix_id.full_id
            and isinstance(content, dict)
            and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
        ):
            return content[ORIGINAL_SENDER_KEY]
        return get_effective_sender_id_for_reply_permissions(event.sender, event.source, self.config)

    def _precheck_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        is_edit: bool = False,
    ) -> str | None:
        """Run common early-exit checks shared by text/media/voice handlers."""
        requester_user_id = self._requester_user_id_for_event(event)

        if requester_user_id == self.matrix_id.full_id:
            return None

        if not is_edit and self.response_tracker.has_responded(event.event_id):
            return None

        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            room_alias=room.canonical_alias,
        ):
            self.response_tracker.mark_responded(event.event_id)
            return None

        if not self._can_reply_to_sender(requester_user_id):
            self.response_tracker.mark_responded(event.event_id)
            return None

        return requester_user_id

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        *,
        requester_user_id: str | None = None,
        event_label: str,
    ) -> _PreparedDispatch | None:
        """Run common precheck/context/sender-gating for dispatch handlers."""
        effective_requester_user_id = requester_user_id or self._precheck_event(room, event)
        if effective_requester_user_id is None:
            return None

        context = await self._extract_message_context(room, event)
        sender_agent_name = extract_agent_name(effective_requester_user_id, self.config)
        if sender_agent_name and not context.am_i_mentioned:
            self.logger.debug(f"Ignoring {event_label} from other agent (not mentioned)")
            return None

        return _PreparedDispatch(
            requester_user_id=effective_requester_user_id,
            context=context,
        )

    async def _resolve_response_action(
        self,
        context: _MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
    ) -> _ResponseAction:
        """Decide whether to respond as a team, individually, or skip."""
        agents_in_thread = get_agents_in_thread(context.thread_history, self.config)
        form_team = await self._decide_team_for_sender(
            agents_in_thread,
            context,
            room,
            requester_user_id,
            message,
            is_dm,
        )

        if form_team.should_form_team and self.matrix_id in form_team.agents:
            first_agent = min(form_team.agents, key=lambda x: x.full_id)
            if self.matrix_id != first_agent:
                return _ResponseAction(kind="skip")
            return _ResponseAction(kind="team", form_team=form_team)

        if not should_agent_respond(
            agent_name=self.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=context.thread_history,
            config=self.config,
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
    ) -> TeamFormationDecision:
        """Decide team formation using only agents the sender is allowed to interact with."""
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(context.thread_history, self.config)
        available_agents_in_room: list[MatrixID] | None = None
        if is_dm:
            available_agents_in_room = get_available_agents_for_sender(room, requester_user_id, self.config)
        return await decide_team_formation(
            self.matrix_id,
            filter_agents_by_sender_permissions(context.mentioned_agents, requester_user_id, self.config),
            filter_agents_by_sender_permissions(agents_in_thread, requester_user_id, self.config),
            filter_agents_by_sender_permissions(all_mentioned_in_thread, requester_user_id, self.config),
            room=room,
            message=message,
            config=self.config,
            is_dm_room=is_dm,
            is_thread=context.is_thread,
            available_agents_in_room=available_agents_in_room,
        )

    async def _extract_message_context(self, room: nio.MatrixRoom, event: _DispatchEvent) -> _MessageContext:
        """Extract mention and thread context for an event."""
        assert self.client is not None

        if _should_skip_mentions(event.source):
            mentioned_agents: list[MatrixID] = []
            am_i_mentioned = False
            has_non_agent_mentions = False
        else:
            mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
                event.source,
                self.matrix_id,
                self.config,
            )

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)

        event_info = EventInfo.from_event(event.source)
        if self.config.get_entity_thread_mode(self.agent_name, room_id=room.room_id) == "room":
            is_thread = False
            thread_id = None
            thread_history: list[dict[str, Any]] = []
        else:
            is_thread, thread_id, thread_history = await self._derive_conversation_context(
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
        )

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
        *,
        agent_name: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> ToolRuntimeContext | None:
        """Build shared runtime context for all tool calls."""
        if self.client is None:
            return None
        return ToolRuntimeContext(
            agent_name=agent_name or self.agent_name,
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id),
            requester_id=user_id or self.matrix_id.full_id,
            client=self.client,
            config=self.config,
            room=self._cached_room(room_id),
            reply_to_event_id=reply_to_event_id,
            storage_path=self.storage_path,
            attachment_ids=tuple(attachment_ids or []),
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
    ) -> str:
        """Append room/thread/event ids to the LLM prompt when messaging tools are available."""
        if not include_context:
            return prompt
        if self._MATRIX_PROMPT_CONTEXT_MARKER in prompt:
            return prompt

        effective_thread_id = self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
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

    def _can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this entity may reply to *sender_id*."""
        return is_sender_allowed_for_agent_reply(sender_id, self.agent_name, self.config)

    async def _dispatch_text_message(
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent,
        requester_user_id: str,
    ) -> None:
        """Run the normal text and command dispatch pipeline for a prepared text event."""
        assert isinstance(event.body, str)

        command = command_parser.parse(event.body)
        if command:
            if self.agent_name == ROUTER_AGENT_NAME:
                await self._handle_command(room, event, command)
            return

        dispatch = await self._prepare_dispatch(
            room,
            event,
            requester_user_id=requester_user_id,
            event_label="message",
        )
        if dispatch is None:
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

        context = dispatch.context
        payload = await self._build_dispatch_payload_with_attachments(
            room_id=room.room_id,
            context=context,
            prompt=event.body,
            current_attachment_ids=message_attachment_ids,
            media_thread_id=context.thread_id,
        )
        await self._execute_dispatch_action(
            room,
            event,
            dispatch,
            action,
            payload,
            processing_log="Processing",
        )

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle incoming text messages for one agent bot."""
        self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
        assert self.client is not None
        if not isinstance(event.body, str) or is_in_progress_message(event.body):
            return

        event_info = EventInfo.from_event(event.source)
        requester_user_id = self._precheck_event(room, event, is_edit=event_info.is_edit)
        if requester_user_id is None:
            return

        if event_info.is_edit:
            await self._handle_message_edit(room, event, event_info)
            return

        await interactive.handle_text_response(self.client, room, event, self.agent_name)
        await self._dispatch_text_message(room, event, requester_user_id)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        assert self.client is not None

        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
            room_alias=room.canonical_alias,
        ):
            self.logger.debug(f"Ignoring reaction from unauthorized sender: {event.sender}")
            return

        if not self._can_reply_to_sender(event.sender):
            self.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
            return

        if event.key == "🛑":
            sender_agent_name = extract_agent_name(event.sender, self.config)
            if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
                self.logger.info(
                    "Stopped generation for message",
                    message_id=event.reacts_to,
                    stopped_by=event.sender,
                )
                await self.stop_manager.remove_stop_button(self.client, event.reacts_to)
                await self._send_response(room.room_id, event.reacts_to, "✅ Generation stopped", None)
                return

        pending_change = config_confirmation.get_pending_change(event.reacts_to)
        if pending_change and self.agent_name == ROUTER_AGENT_NAME:
            await config_confirmation.handle_confirmation_reaction(self, room, event, pending_change)
            return

        result = await interactive.handle_reaction(self.client, event, self.agent_name, self.config)
        if result:
            selected_value, thread_id = result
            thread_history = []
            if thread_id:
                thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)
                if has_user_responded_after_message(thread_history, event.reacts_to, self.matrix_id):
                    self.logger.info(
                        "Ignoring reaction - agent already responded after this question",
                        reacted_to=event.reacts_to,
                    )
                    return

            ack_text = f"You selected: {event.key} {selected_value}\n\nProcessing your response..."
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
            response_event_id = await self._generate_response(
                room_id=room.room_id,
                prompt=prompt,
                reply_to_event_id=event.reacts_to,
                thread_id=thread_id,
                thread_history=thread_history,
                existing_event_id=ack_event_id,
                user_id=event.sender,
            )
            self.response_tracker.mark_responded(event.reacts_to, response_event_id)

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
        """Resolve routing plus team or individual action for a prepared dispatch."""
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
        payload: _DispatchPayload,
        *,
        processing_log: str,
    ) -> None:
        """Execute resolved dispatch action and mark the source event responded."""
        if action.kind == "team":
            assert action.form_team is not None
            response_event_id = await self._generate_team_response_helper(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=dispatch.context.thread_id,
                payload=payload,
                team_agents=action.form_team.agents,
                team_mode=action.form_team.mode,
                thread_history=dispatch.context.thread_history,
                requester_user_id=dispatch.requester_user_id,
                existing_event_id=None,
            )
            self.response_tracker.mark_responded(event.event_id, response_event_id)
            return

        if not dispatch.context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        self.logger.info(processing_log, event_id=event.event_id)
        response_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=payload.prompt,
            reply_to_event_id=event.event_id,
            thread_id=dispatch.context.thread_id,
            thread_history=dispatch.context.thread_history,
            user_id=dispatch.requester_user_id,
            media=payload.media,
            attachment_ids=payload.attachment_ids,
        )
        self.response_tracker.mark_responded(event.event_id, response_event_id)

    async def _handle_command(
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent,
        command: Command,
    ) -> None:
        """Handle a parsed router command."""
        assert self.client is not None
        context = CommandHandlerContext(
            client=self.client,
            config=self.config,
            logger=self.logger,
            response_tracker=self.response_tracker,
            derive_conversation_context=self._derive_conversation_context,
            requester_user_id_for_event=self._requester_user_id_for_event,
            resolve_reply_thread_id=self._resolve_reply_thread_id,
            send_response=self._send_response,
            send_skill_command_response=self._send_skill_command_response,
        )
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
        )

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
        event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    ) -> None:
        """Normalize audio into a synthetic text event and reuse text dispatch."""
        assert self.client is not None

        requester_user_id = self._precheck_event(room, event)
        if requester_user_id is None:
            return

        if is_agent_id(event.sender, self.config):
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
            _SyntheticTextEvent(
                sender=event.sender,
                event_id=event.event_id,
                body=prepared_voice.text,
                source=prepared_voice.source,
            ),
            requester_user_id,
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

        if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
            await self._on_audio_media_message(room, event)
            return

        is_image_event = isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage)
        default_caption = (
            "[Attached image]"
            if is_image_event
            else "[Attached video]"
            if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo)
            else "[Attached file]"
        )
        caption = extract_media_caption(event, default=default_caption)

        dispatch = await self._prepare_dispatch(
            room,
            event,
            event_label="image" if is_image_event else "media",
        )
        if dispatch is None:
            return

        context = dispatch.context
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
            image = await image_handler.download_image(self.client, event)
            if image is None:
                self.logger.error("Failed to download image", event_id=event.event_id)
                self.response_tracker.mark_responded(event.event_id)
                return
            attachment_record = await register_image_attachment(
                self.client,
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
                self.client,
                self.storage_path,
                room_id=room.room_id,
                thread_id=effective_thread_id,
                event=event,
            )
            if attachment_record is None:
                self.logger.error("Failed to register media attachment", event_id=event.event_id)
                self.response_tracker.mark_responded(event.event_id)
                return
            current_attachment_ids = [attachment_record.attachment_id]
        payload = await self._build_dispatch_payload_with_attachments(
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
            payload,
            processing_log="Processing image" if is_image_event else "Processing media message",
        )

    async def _register_routed_attachment(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        event: _MediaDispatchEvent,
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
        """Run the router dispatch logic shared by text and media handlers."""
        if self.agent_name != ROUTER_AGENT_NAME:
            return _RouterDispatchResult(handled=False)

        agents_in_thread = get_agents_in_thread(context.thread_history, self.config)
        sender_visible = filter_agents_by_sender_permissions(agents_in_thread, requester_user_id, self.config)

        if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
            if context.is_thread and has_multiple_non_agent_users_in_thread(context.thread_history, self.config):
                self.logger.info("Skipping routing: multiple non-agent users in thread (mention required)")
                return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)
            available_agents = get_available_agents_for_sender(room, requester_user_id, self.config)
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

    async def _handle_ai_routing(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        thread_history: list[dict[str, Any]],
        thread_id: str | None = None,
        message: str | None = None,
        requester_user_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> None:
        """Suggest an agent and relay that routing decision into the room."""
        assert self.agent_name == ROUTER_AGENT_NAME

        permission_sender_id = requester_user_id or event.sender
        available_agents = get_configured_agents_for_room(room.room_id, self.config)
        available_agents = filter_agents_by_sender_permissions(available_agents, permission_sender_id, self.config)
        if not available_agents:
            self.logger.debug("No configured agents to route to in this room for sender", sender=permission_sender_id)
            return

        self.logger.info("Handling AI routing", event_id=event.event_id)

        routing_text = message or event.body
        suggested_agent = await suggest_agent_for_message(
            routing_text,
            available_agents,
            self.config,
            thread_history,
        )

        if not suggested_agent:
            response_text = (
                "⚠️ I couldn't determine which agent should help with this. "
                "Please try mentioning an agent directly with @ or rephrase your request."
            )
            self.logger.warning("Router failed to determine agent")
        else:
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = (
            self.config.get_entity_thread_mode(suggested_agent, room_id=room.room_id) if suggested_agent else None
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

    async def _generate_team_response_helper(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        team_agents: list[MatrixID],
        team_mode: str,
        thread_history: list[dict],
        requester_user_id: str,
        existing_event_id: str | None = None,
        *,
        payload: _DispatchPayload,
    ) -> str | None:
        """Generate a team response shared between preformed teams and TeamBot."""
        assert self.client is not None

        model_name = select_model_for_team(self.agent_name, room_id, self.config)
        room_mode = self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room"
        use_streaming = await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=requester_user_id,
            enable_streaming=self.enable_streaming,
        )
        mode = TeamMode.COORDINATE if team_mode == "coordinate" else TeamMode.COLLABORATE
        agent_names = [mid.agent_name(self.config) or mid.username for mid in team_agents]
        include_matrix_prompt_context = any(self._agent_has_matrix_messaging_tool(name) for name in agent_names)
        model_message = self._append_matrix_prompt_context(
            payload.prompt,
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            include_context=include_matrix_prompt_context,
        )
        tool_context = self._build_tool_runtime_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=requester_user_id,
            attachment_ids=payload.attachment_ids,
        )
        orchestrator = self.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)

        client = self.client

        async def generate_team_response(message_id: str | None) -> None:
            if use_streaming and not existing_event_id:
                async with typing_indicator(client, room_id):
                    with tool_runtime_context(tool_context):
                        response_stream = team_response_stream(
                            agent_ids=team_agents,
                            message=model_message,
                            orchestrator=orchestrator,
                            mode=mode,
                            thread_history=thread_history,
                            model_name=model_name,
                            media=payload.media,
                            show_tool_calls=self.show_tool_calls,
                        )

                        event_id, accumulated = await send_streaming_response(
                            client,
                            room_id,
                            reply_to_event_id,
                            thread_id,
                            self.matrix_id.domain,
                            self.config,
                            response_stream,
                            streaming_cls=ReplacementStreamingResponse,
                            header=None,
                            show_tool_calls=self.show_tool_calls,
                            existing_event_id=message_id,
                            room_mode=room_mode,
                        )

                await self._handle_interactive_question(
                    event_id,
                    accumulated,
                    room_id,
                    thread_id,
                    reply_to_event_id,
                    agent_name="team",
                )
            else:
                async with typing_indicator(client, room_id):
                    with tool_runtime_context(tool_context):
                        response_text = await team_response(
                            agent_names=agent_names,
                            mode=mode,
                            message=model_message,
                            orchestrator=orchestrator,
                            thread_history=thread_history,
                            model_name=model_name,
                            media=payload.media,
                        )

                if message_id:
                    await self._edit_message(room_id, message_id, response_text, thread_id)
                else:
                    event_id = await self._send_response(
                        room_id,
                        reply_to_event_id,
                        response_text,
                        thread_id,
                    )
                    if event_id:
                        await self._handle_interactive_question(
                            event_id,
                            response_text,
                            room_id,
                            thread_id,
                            reply_to_event_id,
                            agent_name="team",
                        )

        thinking_msg = None if existing_event_id else "🤝 Team Response: Thinking..."
        return await self._run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            response_function=generate_team_response,
            thinking_message=thinking_msg,
            existing_event_id=existing_event_id,
            user_id=requester_user_id,
        )

    async def _run_cancellable_response(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_function: Callable[[str | None], Coroutine[object, object, None]],
        thinking_message: str | None = None,
        existing_event_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """Run a response generation function with cancellation support."""
        assert self.client is not None
        assert not (thinking_message and existing_event_id), (
            "thinking_message and existing_event_id are mutually exclusive"
        )

        initial_message_id = None
        if thinking_message:
            initial_message_id = await self._send_response(
                room_id,
                reply_to_event_id,
                f"{thinking_message} {IN_PROGRESS_MARKER}",
                thread_id,
            )

        message_id = existing_event_id or initial_message_id
        task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))

        message_to_track = existing_event_id or initial_message_id
        show_stop_button = False

        if message_to_track:
            self.stop_manager.set_current(message_to_track, room_id, task, None)

            show_stop_button = self.config.defaults.show_stop_button
            if show_stop_button and user_id:
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
            self.logger.info("Response cancelled by user", message_id=message_to_track)
        except Exception as exc:
            self.logger.exception("Error during response generation", error=str(exc))
            raise
        finally:
            if message_to_track:
                tracked = self.stop_manager.tracked_messages.get(message_to_track)
                button_already_removed = tracked is None or tracked.reaction_event_id is None
                self.stop_manager.clear_message(
                    message_to_track,
                    client=self.client,
                    remove_button=show_stop_button and not button_already_removed,
                )

        return initial_message_id

    async def _process_and_respond(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """Process a message and send a response without streaming."""
        assert self.client is not None
        if not prompt.strip():
            return None

        media_inputs = media or MediaInputs()
        session_id = create_session_id(room_id, thread_id)
        knowledge = self._knowledge_for_agent(self.agent_name)
        model_prompt = self._append_matrix_prompt_context(
            prompt,
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
        )
        tool_context = self._build_tool_runtime_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
            attachment_ids=attachment_ids,
        )
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}

        try:
            async with typing_indicator(self.client, room_id):
                with tool_runtime_context(tool_context):
                    response_text = await ai_response(
                        agent_name=self.agent_name,
                        prompt=model_prompt,
                        session_id=session_id,
                        storage_path=self.storage_path,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        media=media_inputs,
                        reply_to_event_id=reply_to_event_id,
                        show_tool_calls=self.show_tool_calls,
                        tool_trace_collector=tool_trace,
                        run_metadata_collector=run_metadata_content,
                    )
        except asyncio.CancelledError:
            self.logger.info("Non-streaming response cancelled by user", message_id=existing_event_id)
            if existing_event_id:
                cancelled_text = "**[Response cancelled by user]**"
                await self._edit_message(room_id, existing_event_id, cancelled_text, thread_id)
            raise

        response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)
        if existing_event_id:
            await self._edit_message(
                room_id,
                existing_event_id,
                response_text,
                thread_id,
                tool_trace=tool_trace if self.show_tool_calls else None,
                extra_content=response_extra_content,
            )
            return existing_event_id

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(
            room_id,
            reply_to_event_id,
            response.formatted_text,
            thread_id,
            tool_trace=tool_trace if self.show_tool_calls else None,
            extra_content=response_extra_content,
        )
        if event_id and response.option_map and response.options_list:
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
                self.agent_name,
            )
            await interactive.add_reaction_buttons(self.client, room_id, event_id, response.options_list)

        return event_id

    async def _send_skill_command_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
    ) -> str | None:
        """Send a skill command response using a specific agent."""
        assert self.client is not None
        if not prompt.strip():
            return None

        session_id = create_session_id(room_id, thread_id)
        reprioritize_auto_flush_sessions(
            self.storage_path,
            self.config,
            agent_name=agent_name,
            active_session_id=session_id,
        )
        knowledge = self._knowledge_for_agent(agent_name)
        model_prompt = self._append_matrix_prompt_context(
            prompt,
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            include_context=self._agent_has_matrix_messaging_tool(agent_name),
        )
        tool_context = self._build_tool_runtime_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
            agent_name=agent_name,
        )
        show_tool_calls = self._show_tool_calls_for_agent(agent_name)
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}

        async with typing_indicator(self.client, room_id):
            with tool_runtime_context(tool_context):
                response_text = await ai_response(
                    agent_name=agent_name,
                    prompt=model_prompt,
                    session_id=session_id,
                    storage_path=self.storage_path,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
                    knowledge=knowledge,
                    reply_to_event_id=reply_to_event_id,
                    show_tool_calls=show_tool_calls,
                    tool_trace_collector=tool_trace,
                    run_metadata_collector=run_metadata_content,
                )

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(
            room_id,
            reply_to_event_id,
            response.formatted_text,
            thread_id,
            reply_to_event=reply_to_event,
            skip_mentions=True,
            tool_trace=tool_trace if show_tool_calls else None,
            extra_content=run_metadata_content or None,
        )

        if event_id and response.option_map and response.options_list:
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
                agent_name=agent_name,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            )
            if self.config.get_agent_memory_backend(agent_name) == "mem0":
                create_background_task(
                    store_conversation_memory(
                        prompt,
                        agent_name,
                        self.storage_path,
                        session_id,
                        self.config,
                        room_id,
                        thread_history,
                        user_id,
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
        """Handle interactive question registration and reactions if present."""
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

    async def _process_and_respond_streaming(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """Process a message and send a response with streaming."""
        assert self.client is not None
        if not prompt.strip():
            return None

        media_inputs = media or MediaInputs()
        session_id = create_session_id(room_id, thread_id)
        knowledge = self._knowledge_for_agent(self.agent_name)
        room_mode = self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room"
        model_prompt = self._append_matrix_prompt_context(
            prompt,
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
        )
        tool_context = self._build_tool_runtime_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
            attachment_ids=attachment_ids,
        )
        run_metadata_content: dict[str, Any] = {}

        try:
            async with typing_indicator(self.client, room_id):
                with tool_runtime_context(tool_context):
                    response_stream = stream_agent_response(
                        agent_name=self.agent_name,
                        prompt=model_prompt,
                        session_id=session_id,
                        storage_path=self.storage_path,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        media=media_inputs,
                        reply_to_event_id=reply_to_event_id,
                        show_tool_calls=self.show_tool_calls,
                        run_metadata_collector=run_metadata_content,
                    )
                    response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)
                    event_id, accumulated = await send_streaming_response(
                        self.client,
                        room_id,
                        reply_to_event_id,
                        thread_id,
                        self.matrix_id.domain,
                        self.config,
                        response_stream,
                        streaming_cls=StreamingResponse,
                        existing_event_id=existing_event_id,
                        room_mode=room_mode,
                        show_tool_calls=self.show_tool_calls,
                        extra_content=response_extra_content,
                    )

            await self._handle_interactive_question(
                event_id,
                accumulated,
                room_id,
                thread_id,
                reply_to_event_id,
            )
        except asyncio.CancelledError:
            self.logger.info("Streaming cancelled by user", message_id=existing_event_id)
            raise
        except Exception as exc:
            self.logger.exception("Error in streaming response", error=str(exc))
            return None
        return event_id

    async def _generate_response(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """Generate and send or edit a response using AI."""
        assert self.client is not None
        media_inputs = media or MediaInputs()
        session_id = create_session_id(room_id, thread_id)
        reprioritize_auto_flush_sessions(
            self.storage_path,
            self.config,
            agent_name=self.agent_name,
            active_session_id=session_id,
        )

        use_streaming = await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=user_id,
            enable_streaming=self.enable_streaming,
        )

        async def generate(message_id: str | None) -> None:
            if use_streaming:
                await self._process_and_respond_streaming(
                    room_id,
                    prompt,
                    reply_to_event_id,
                    thread_id,
                    thread_history,
                    message_id,
                    user_id=user_id,
                    media=media_inputs,
                    attachment_ids=attachment_ids,
                )
            else:
                await self._process_and_respond(
                    room_id,
                    prompt,
                    reply_to_event_id,
                    thread_id,
                    thread_history,
                    message_id,
                    user_id=user_id,
                    media=media_inputs,
                    attachment_ids=attachment_ids,
                )

        thinking_msg = None if existing_event_id else "Thinking..."
        event_id = await self._run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            response_function=generate,
            thinking_message=thinking_msg,
            existing_event_id=existing_event_id,
            user_id=user_id,
        )

        try:
            mark_auto_flush_dirty_session(
                self.storage_path,
                self.config,
                agent_name=self.agent_name,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            )
            if self.config.get_agent_memory_backend(self.agent_name) == "mem0":
                create_background_task(
                    store_conversation_memory(
                        prompt,
                        self.agent_name,
                        self.storage_path,
                        session_id,
                        self.config,
                        room_id,
                        thread_history,
                        user_id,
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

        return event_id

    async def _handle_message_edit(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        event_info: EventInfo,
    ) -> None:
        """Handle an edited message by regenerating the agent's response."""
        if not event_info.original_event_id:
            self.logger.debug("Edit event has no original event ID")
            return

        sender_agent_name = extract_agent_name(event.sender, self.config)
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
        requester_user_id = self._requester_user_id_for_event(event)

        should_respond_to_edit = should_agent_respond(
            agent_name=self.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=context.thread_history,
            config=self.config,
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            sender_id=requester_user_id,
        )
        if not should_respond_to_edit:
            self.logger.debug("Agent should not respond to edited message")
            return

        edited_content = event.source["content"]["m.new_content"]["body"]

        storage = create_session_storage(self.agent_name, self.storage_path)
        session_ids_to_check = [
            create_session_id(room.room_id, context.thread_id),
            create_session_id(room.room_id, None),
        ]
        checked_session_ids: set[str] = set()
        for session_id in session_ids_to_check:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            removed = remove_run_by_event_id(storage, session_id, event_info.original_event_id)
            if removed:
                self.logger.info(
                    "Removed stale run for edited message",
                    event_id=event_info.original_event_id,
                    session_id=session_id,
                )

        await self._generate_response(
            room_id=room.room_id,
            prompt=edited_content,
            reply_to_event_id=event_info.original_event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            user_id=requester_user_id,
        )

        self.response_tracker.mark_responded(event_info.original_event_id, response_event_id)
        self.logger.info("Successfully regenerated response for edited message")


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
        thread_history: list[dict],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
    ) -> None:
        """Generate a team response instead of an individual agent response."""
        if not prompt.strip():
            return

        assert self.client is not None

        session_id = create_session_id(room_id, thread_id)
        agent_names = [mid.agent_name(self.config) or mid.username for mid in self.team_agents]
        create_background_task(
            store_conversation_memory(
                prompt,
                agent_names,
                self.storage_path,
                session_id,
                self.config,
                room_id,
                thread_history,
                user_id,
            ),
            name=f"memory_save_team_{session_id}",
        )
        self.logger.info(f"Storing memory for team: {agent_names}")

        media_inputs = media or MediaInputs()

        await self._generate_team_response_helper(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            payload=_DispatchPayload(
                prompt=prompt,
                media=media_inputs,
                attachment_ids=attachment_ids,
            ),
            team_agents=self.team_agents,
            team_mode=self.team_mode,
            thread_history=thread_history,
            requester_user_id=user_id or "",
            existing_event_id=existing_event_id,
        )
