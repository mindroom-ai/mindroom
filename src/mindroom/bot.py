"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import nio
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential

from . import config_confirmation, image_handler, interactive, voice_handler
from .agents import create_agent, create_session_storage, remove_run_by_event_id
from .ai import ai_response, stream_agent_response
from .background_tasks import create_background_task, wait_for_background_tasks
from .command_handler import CommandHandlerContext, _generate_welcome_message, handle_command
from .commands import Command, command_parser
from .constants import MATRIX_HOMESERVER, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from .knowledge_utils import MultiKnowledgeVectorDb, resolve_agent_knowledge
from .logging_config import emoji, get_logger
from .matrix.avatar import check_and_set_avatar
from .matrix.client import (
    _latest_thread_event_id,
    edit_message,
    fetch_thread_history,
    get_joined_rooms,
    get_latest_thread_event_id_if_needed,
    join_room,
    send_message,
)
from .matrix.event_info import EventInfo
from .matrix.identity import (
    MatrixID,
    extract_agent_name,
)
from .matrix.mentions import format_message_with_mentions
from .matrix.presence import build_agent_status_message, is_user_online, set_presence_status, should_use_streaming
from .matrix.reply_chain import ReplyChainCaches, derive_conversation_context
from .matrix.rooms import (
    is_dm_room,
    leave_non_dm_rooms,
    resolve_room_aliases,
)
from .matrix.typing import typing_indicator
from .matrix.users import (
    AgentMatrixUser,
    create_agent_user,
    login_agent_user,
)
from .memory import store_conversation_memory
from .memory.auto_flush import (
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)
from .openclaw_context import OpenClawToolContext, openclaw_tool_context
from .response_tracker import ResponseTracker
from .room_cleanup import cleanup_all_orphaned_bots
from .routing import suggest_agent_for_message
from .scheduling import (
    restore_scheduled_tasks,
)
from .scheduling_context import SchedulingToolContext, scheduling_tool_context
from .stop import StopManager
from .streaming import (
    IN_PROGRESS_MARKER,
    ReplacementStreamingResponse,
    StreamingResponse,
    is_in_progress_message,
    send_streaming_response,
)
from .teams import (
    TeamFormationDecision,
    TeamMode,
    decide_team_formation,
    select_model_for_team,
    team_response,
    team_response_stream,
)
from .thread_utils import (
    check_agent_mentioned,
    create_session_id,
    filter_agents_by_sender_permissions,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_available_agents_for_sender,
    get_configured_agents_for_room,
    get_effective_sender_id_for_reply_permissions,
    has_multiple_non_agent_users_in_thread,
    has_user_responded_after_message,
    is_authorized_sender,
    is_sender_allowed_for_agent_reply,
    should_agent_respond,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import structlog
    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.media import Image

    from .config.main import Config
    from .orchestrator import MultiAgentOrchestrator
    from .tool_events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = ["AgentBot", "MultiKnowledgeVectorDb"]


# Constants
SYNC_TIMEOUT_MS = 30000


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

    kind: Literal["skip", "team", "individual"]
    form_team: TeamFormationDecision | None = None


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
    return bool(content.get("com.mindroom.skip_mentions", False))


def create_bot_for_entity(
    entity_name: str,
    agent_user: AgentMatrixUser,
    config: Config,
    storage_path: Path,
) -> AgentBot | TeamBot | None:
    """Create appropriate bot instance for an entity (agent, team, or router).

    Args:
        entity_name: Name of the entity to create a bot for
        agent_user: Matrix user for the bot
        config: Configuration object
        storage_path: Path for storing agent data

    Returns:
        Bot instance or None if entity not found in config

    """
    enable_streaming = config.defaults.enable_streaming

    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        # Convert team member agent names into canonical agent Matrix IDs.
        # Team streaming resolves config agents from these IDs, so they must keep
        # the `mindroom_` prefix used by MatrixID.from_agent().
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
class MessageContext:
    """Context extracted from a Matrix message event."""

    am_i_mentioned: bool
    is_thread: bool
    thread_id: str | None
    thread_history: list[dict]
    mentioned_agents: list[MatrixID]
    has_non_agent_mentions: bool


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    agent_user: AgentMatrixUser
    storage_path: Path
    config: Config
    rooms: list[str] = field(default_factory=list)

    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    enable_streaming: bool = field(default=True)  # Enable/disable streaming responses
    orchestrator: MultiAgentOrchestrator | None = field(default=None, init=False)  # Reference to orchestrator
    _reply_chain: ReplyChainCaches = field(default_factory=ReplyChainCaches, init=False)

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

    @property
    def thread_mode(self) -> Literal["thread", "room"]:
        """Get the thread mode for this agent."""
        return self.config.get_entity_thread_mode(self.agent_name)

    def _resolve_reply_thread_id(
        self,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        *,
        event_source: dict[str, Any] | None = None,
        thread_mode_override: Literal["thread", "room"] | None = None,
    ) -> str | None:
        """Resolve the effective thread root for outgoing replies.

        In room mode this always returns ``None`` so callers send plain room
        messages and store room-level state. In thread mode, this prefers an
        existing thread ID and falls back to a safe root/reply target.
        """
        effective_thread_mode = thread_mode_override or self.thread_mode
        if effective_thread_mode == "room":
            return None
        event_info = EventInfo.from_event(event_source)
        return thread_id or event_info.safe_thread_root or reply_to_event_id

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

    @property  # Not cached_property because Team mutates it!
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
        # Use the tracking subdirectory, not the root storage path
        tracking_dir = self.storage_path / "tracking"
        return ResponseTracker(self.agent_name, base_path=tracking_dir)

    @cached_property
    def stop_manager(self) -> StopManager:
        """Get or create the StopManager for this agent."""
        return StopManager()

    async def _fetch_thread_images(self, room_id: str, thread_id: str) -> list[Image]:
        """Download images from the thread root event, if it is an image message."""
        assert self.client is not None
        response = await self.client.room_get_event(room_id, thread_id)
        if not isinstance(response, nio.RoomGetEventResponse):
            return []
        event = response.event
        if not isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
            return []
        img = await image_handler.download_image(self.client, event)
        return [img] if img else []

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        assert self.client is not None
        for room_id in self.rooms:
            if await join_room(self.client, room_id):
                self.logger.info("Joined room", room_id=room_id)
                # Only the router agent should restore scheduled tasks
                # to avoid duplicate task instances after restart
                if self.agent_name == ROUTER_AGENT_NAME:
                    # Restore scheduled tasks
                    restored_tasks = await restore_scheduled_tasks(self.client, room_id, self.config)
                    if restored_tasks > 0:
                        self.logger.info(f"Restored {restored_tasks} scheduled tasks in room {room_id}")

                    # Restore pending config confirmations
                    restored_configs = await config_confirmation.restore_pending_changes(self.client, room_id)
                    if restored_configs > 0:
                        self.logger.info(f"Restored {restored_configs} pending config changes in room {room_id}")

                    # Send welcome message if room is empty
                    await self._send_welcome_message_if_empty(room_id)
            else:
                self.logger.warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for."""
        assert self.client is not None

        # Get all rooms we're currently in
        joined_rooms = await get_joined_rooms(self.client)
        if joined_rooms is None:
            return

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.rooms)

        # Leave rooms we're no longer configured for (preserving DM rooms)
        await leave_non_dm_rooms(self.client, list(current_rooms - configured_rooms))

    async def ensure_user_account(self) -> None:
        """Ensure this agent has a Matrix user account.

        This method makes the agent responsible for its own user account creation,
        moving this responsibility from the orchestrator to the agent itself.
        """
        # If we already have a user_id (e.g., provided by tests or config), assume account exists
        if getattr(self.agent_user, "user_id", ""):
            return
        # Create or retrieve the Matrix user account
        self.agent_user = await create_agent_user(
            MATRIX_HOMESERVER,
            self.agent_name,
            self.agent_user.display_name,  # Use existing display name if available
        )
        self.logger.info(f"Ensured Matrix user account: {self.agent_user.user_id}")

    async def _set_avatar_if_available(self) -> None:
        """Set avatar for the agent if an avatar file exists."""
        if not self.client:
            return

        entity_type = "teams" if self.agent_name in self.config.teams else "agents"
        avatar_path = Path(__file__).parent.parent.parent / "avatars" / entity_type / f"{self.agent_name}.png"

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
        self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)
        await self._set_avatar_if_available()
        await self._set_presence_with_model_info()

        # Register event callbacks - wrap them to run as background tasks
        # This ensures the sync loop is never blocked, allowing stop reactions to work
        self.client.add_event_callback(_create_task_wrapper(self._on_invite), nio.InviteEvent)  # ty: ignore[invalid-argument-type]  # InviteEvent doesn't inherit Event
        self.client.add_event_callback(_create_task_wrapper(self._on_message), nio.RoomMessageText)
        self.client.add_event_callback(_create_task_wrapper(self._on_reaction), nio.ReactionEvent)

        # Register voice message callbacks (only for router agent to avoid duplicates)
        if self.agent_name == ROUTER_AGENT_NAME:
            self.client.add_event_callback(_create_task_wrapper(self._on_voice_message), nio.RoomMessageAudio)
            self.client.add_event_callback(_create_task_wrapper(self._on_voice_message), nio.RoomEncryptedAudio)

        # Register image message callbacks on all agents (each agent handles its own routing)
        self.client.add_event_callback(_create_task_wrapper(self._on_image_message), nio.RoomMessageImage)
        self.client.add_event_callback(_create_task_wrapper(self._on_image_message), nio.RoomEncryptedImage)

        self.running = True

        # Router bot has additional responsibilities
        if self.agent_name == ROUTER_AGENT_NAME:
            try:
                await cleanup_all_orphaned_bots(self.client, self.config)
            except Exception as e:
                self.logger.warning(f"Could not cleanup orphaned bots (non-critical): {e}")

        # Note: Room joining is deferred until after invitations are handled
        self.logger.info(f"Agent setup complete: {self.agent_user.user_id}")

    async def try_start(self) -> bool:
        """Try to start the agent bot with smart retry logic.

        Uses tenacity to retry transient failures (network, timeouts) but not
        permanent ones (auth failures).

        Returns:
            True if the bot started successfully, False otherwise.

        """

        def should_retry_error(retry_state: RetryCallState) -> bool:
            """Determine if we should retry based on the exception.

            Don't retry on auth failures (M_FORBIDDEN, M_USER_DEACTIVATED, etc)
            which come as ValueError with those strings in the message.
            """
            if retry_state.outcome is None:
                return True
            exception = retry_state.outcome.exception()
            if exception is None:
                return False

            # Don't retry auth failures
            if isinstance(exception, ValueError):
                error_msg = str(exception)
                # Matrix auth error codes that shouldn't be retried
                permanent_errors = ["M_FORBIDDEN", "M_USER_DEACTIVATED", "M_UNKNOWN_TOKEN", "M_INVALID_USERNAME"]
                return not any(err in error_msg for err in permanent_errors)

            # Retry other exceptions (network errors, timeouts, etc)
            return True

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=should_retry_error,
            reraise=True,
        )
        async def _start_with_retry() -> None:
            await self.start()

        try:
            await _start_with_retry()
            return True  # noqa: TRY300
        except Exception:
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
        await self.stop()

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False

        # Wait for any pending background tasks (like memory saves) to complete
        try:
            await wait_for_background_tasks(timeout=5.0)  # 5 second timeout
            self.logger.info("Background tasks completed")
        except Exception as e:
            self.logger.warning(f"Some background tasks did not complete: {e}")

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
            # Check if the only message is our welcome message
            msg = response.chunk[0]
            if (
                hasattr(msg, "sender")
                and msg.sender == self.agent_user.user_id
                and hasattr(msg, "body")
                and "Welcome to MindRoom" in msg.body
            ):
                self.logger.debug("Welcome message already sent", room_id=room_id)
                return
            # Otherwise, room has a different message, don't send welcome
        # Room has other messages, don't send welcome

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        await self.client.sync_forever(timeout=SYNC_TIMEOUT_MS, full_state=True)

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

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:  # noqa: C901, PLR0911
        self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
        assert self.client is not None
        if not isinstance(event.body, str) or is_in_progress_message(event.body):
            return

        event_info = EventInfo.from_event(event.source)
        requester_user_id = self._precheck_event(room, event, is_edit=event_info.is_edit)
        if requester_user_id is None:
            return

        # Handle edit events
        if event_info.is_edit:
            await self._handle_message_edit(room, event, event_info)
            return

        # We only receive events from rooms we're in - no need to check access
        _is_dm_room = await is_dm_room(self.client, room.room_id)

        await interactive.handle_text_response(self.client, room, event, self.agent_name)

        # Router handles commands exclusively
        command = command_parser.parse(event.body)
        if command:
            if self.agent_name == ROUTER_AGENT_NAME:
                # Router always handles commands, even in single-agent rooms
                # Commands like !schedule, !help, etc. need to work regardless
                await self._handle_command(room, event, command)
            return

        context = await self._extract_message_context(room, event)

        # Check if the sender is an agent
        sender_agent_name = extract_agent_name(requester_user_id, self.config)

        # Skip unmentioned messages authored by agents. Relayed messages carrying
        # original-sender metadata resolve to that user before this check.
        if sender_agent_name and not context.am_i_mentioned:
            self.logger.debug("Ignoring message from other agent (not mentioned)")
            return

        # Router dispatch (routing / skip) — shared with image handler
        if await self._handle_router_dispatch(room, event, context, requester_user_id):
            return

        # Decide: team response, individual response, or skip
        action = await self._resolve_response_action(
            context,
            room,
            requester_user_id,
            event.body,
            _is_dm_room,
        )
        if action.kind == "skip":
            return

        if action.kind == "team":
            assert action.form_team is not None
            response_event_id = await self._generate_team_response_helper(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=context.thread_id,
                message=event.body,
                team_agents=action.form_team.agents,
                team_mode=action.form_team.mode,
                thread_history=context.thread_history,
                requester_user_id=requester_user_id,
                existing_event_id=None,
            )
            self.response_tracker.mark_responded(event.event_id, response_event_id)
            return

        # Individual response
        if not context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        self.logger.info("Processing", event_id=event.event_id)

        # If responding in a thread, check whether the thread root is an image
        # so the model can actually see it (e.g. after router routes an image).
        thread_images = await self._fetch_thread_images(room.room_id, context.thread_id) if context.thread_id else []

        response_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=event.body,
            reply_to_event_id=event.event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            user_id=requester_user_id,
            images=thread_images or None,
        )
        self.response_tracker.mark_responded(event.event_id, response_event_id)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        assert self.client is not None

        # Check if sender is authorized to interact with agents
        if not is_authorized_sender(
            event.sender,
            self.config,
            room.room_id,
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
            sender_agent_name = extract_agent_name(event.sender, self.config)
            # Only handle stop from users, not agents, and only if tracking this message
            if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
                self.logger.info(
                    "Stopped generation for message",
                    message_id=event.reacts_to,
                    stopped_by=event.sender,
                )
                # Remove the stop button immediately for user feedback
                await self.stop_manager.remove_stop_button(self.client, event.reacts_to)
                # Send a confirmation message
                await self._send_response(room.room_id, event.reacts_to, "✅ Generation stopped", None)
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

        result = await interactive.handle_reaction(self.client, event, self.agent_name, self.config)

        if result:
            selected_value, thread_id = result
            # User selected an option from an interactive question

            # Check if we should process this reaction
            thread_history = []
            if thread_id:
                thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)
                if has_user_responded_after_message(thread_history, event.reacts_to, self.matrix_id):
                    self.logger.info(
                        "Ignoring reaction - agent already responded after this question",
                        reacted_to=event.reacts_to,
                    )
                    return

            # Send immediate acknowledgment
            ack_text = f"You selected: {event.key} {selected_value}\n\nProcessing your response..."
            # Matrix doesn't allow reply relations to events that already have relations (reactions)
            # In threads, omit reply_to_event_id; the thread_id ensures correct placement
            ack_event_id = await self._send_response(
                room.room_id,
                None if thread_id else event.reacts_to,
                ack_text,
                thread_id,
            )

            if not ack_event_id:
                self.logger.error("Failed to send acknowledgment for reaction")
                return

            # Generate the response, editing the acknowledgment message
            # Note: existing_event_id is only used for interactive questions to edit the acknowledgment
            prompt = f"The user selected: {selected_value}"
            response_event_id = await self._generate_response(
                room_id=room.room_id,
                prompt=prompt,
                reply_to_event_id=event.reacts_to,
                thread_id=thread_id,
                thread_history=thread_history,
                existing_event_id=ack_event_id,  # Edit the acknowledgment instead of creating new message
                user_id=event.sender,
            )
            # Mark the original interactive question as responded
            self.response_tracker.mark_responded(event.reacts_to, response_event_id)

    async def _on_voice_message(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    ) -> None:
        """Handle voice message events for transcription and processing."""
        assert self.client is not None
        if not self.config.voice.enabled:
            return

        if self._precheck_event(room, event) is None:
            return

        self.logger.info("Processing voice message", event_id=event.event_id, sender=event.sender)

        transcribed_message = await voice_handler.handle_voice_message(self.client, room, event, self.config)

        if transcribed_message:
            event_info = EventInfo.from_event(event.source)
            _, thread_id, _ = await self._derive_conversation_context(room.room_id, event_info)
            response_event_id = await self._send_response(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text=transcribed_message,
                thread_id=thread_id,
                extra_content={ORIGINAL_SENDER_KEY: event.sender},
            )
            self.response_tracker.mark_responded(event.event_id, response_event_id)
        else:
            # Mark as responded to avoid reprocessing
            self.response_tracker.mark_responded(event.event_id)

    async def _on_image_message(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageImage | nio.RoomEncryptedImage,
    ) -> None:
        """Handle image message events by passing the image to the AI model."""
        assert self.client is not None

        requester_user_id = self._precheck_event(room, event)
        if requester_user_id is None:
            return

        # Skip messages from other agents unless mentioned
        sender_agent_name = extract_agent_name(requester_user_id, self.config)
        context = await self._extract_message_context(room, event)

        if sender_agent_name and not context.am_i_mentioned:
            self.logger.debug("Ignoring image from other agent (not mentioned)")
            return

        # Router dispatch (routing / skip) — shared with text handler
        caption = image_handler.extract_caption(event)
        if await self._handle_router_dispatch(room, event, context, requester_user_id, message=caption):
            return

        # Decide: team response, individual response, or skip (before downloading)
        _is_dm_room = await is_dm_room(self.client, room.room_id)
        action = await self._resolve_response_action(
            context,
            room,
            requester_user_id,
            event.body,
            _is_dm_room,
        )
        if action.kind == "skip":
            return

        # Download image only after confirming we should respond
        image = await image_handler.download_image(self.client, event)
        if image is None:
            self.logger.error("Failed to download image", event_id=event.event_id)
            self.response_tracker.mark_responded(event.event_id)
            return

        self.logger.info("Processing image", event_id=event.event_id)

        if action.kind == "team":
            assert action.form_team is not None
            response_event_id = await self._generate_team_response_helper(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=context.thread_id,
                message=caption,
                team_agents=action.form_team.agents,
                team_mode=action.form_team.mode,
                thread_history=context.thread_history,
                requester_user_id=requester_user_id,
                existing_event_id=None,
                images=[image],
            )
        else:
            response_event_id = await self._generate_response(
                room_id=room.room_id,
                prompt=caption,
                reply_to_event_id=event.event_id,
                thread_id=context.thread_id,
                thread_history=context.thread_history,
                user_id=requester_user_id,
                images=[image],
            )
        self.response_tracker.mark_responded(event.event_id, response_event_id)

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

    def _requester_user_id_for_event(
        self,
        event: nio.RoomMessageText
        | nio.RoomMessageImage
        | nio.RoomEncryptedImage
        | nio.RoomMessageAudio
        | nio.RoomEncryptedAudio,
    ) -> str:
        """Return the effective requester for per-user reply checks."""
        return get_effective_sender_id_for_reply_permissions(event.sender, event.source, self.config)

    def _precheck_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText
        | nio.RoomMessageImage
        | nio.RoomEncryptedImage
        | nio.RoomMessageAudio
        | nio.RoomEncryptedAudio,
        *,
        is_edit: bool = False,
    ) -> str | None:
        """Common early-exit checks shared by text, image, and voice handlers.

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
            room_alias=room.canonical_alias,
        ):
            self.response_tracker.mark_responded(event.event_id)
            return None

        if not self._can_reply_to_sender(requester_user_id):
            self.response_tracker.mark_responded(event.event_id)
            return None

        return requester_user_id

    def _can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this entity may reply to *sender_id*."""
        return is_sender_allowed_for_agent_reply(sender_id, self.agent_name, self.config)

    async def _handle_router_dispatch(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText | nio.RoomMessageImage | nio.RoomEncryptedImage,
        context: MessageContext,
        requester_user_id: str,
        *,
        message: str | None = None,
    ) -> bool:
        """Run the router dispatch logic shared by text and image handlers.

        Returns True when this agent is the router and has handled (or skipped)
        the message, meaning the caller should ``return`` immediately.
        """
        if self.agent_name != ROUTER_AGENT_NAME:
            return False

        agents_in_thread = get_agents_in_thread(context.thread_history, self.config)
        sender_visible = filter_agents_by_sender_permissions(agents_in_thread, requester_user_id, self.config)

        if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
            if context.is_thread and has_multiple_non_agent_users_in_thread(context.thread_history, self.config):
                self.logger.info("Skipping routing: multiple non-agent users in thread (mention required)")
            else:
                available_agents = get_available_agents_for_sender(room, requester_user_id, self.config)
                if len(available_agents) == 1:
                    self.logger.info("Skipping routing: only one agent present")
                else:
                    await self._handle_ai_routing(
                        room,
                        event,
                        context.thread_history,
                        context.thread_id,
                        message=message,
                        requester_user_id=requester_user_id,
                    )
        return True

    async def _resolve_response_action(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        requester_user_id: str,
        message: str,
        is_dm: bool,
    ) -> _ResponseAction:
        """Decide whether to respond as a team, individually, or skip.

        Shared by text and image handlers to avoid duplicating the team
        formation + should-respond decision.
        """
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
        context: MessageContext,
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

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessage) -> MessageContext:
        assert self.client is not None

        # Check if mentions should be ignored for this message
        skip_mentions = _should_skip_mentions(event.source)

        if skip_mentions:
            # Don't detect mentions if the message has skip_mentions metadata
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
            self.logger.info("Mentioned", event_id=event.event_id, room_name=room.name)

        event_info = EventInfo.from_event(event.source)
        if self.thread_mode == "room":
            is_thread = False
            thread_id = None
            thread_history: list[dict[str, Any]] = []
        else:
            is_thread, thread_id, thread_history = await self._derive_conversation_context(
                room.room_id,
                event_info,
            )

        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
        )

    def _build_scheduling_tool_context(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str,
        user_id: str | None,
    ) -> SchedulingToolContext | None:
        """Build runtime context for scheduler tool calls during response generation."""
        client = self.client
        if client is None:
            self.logger.warning("No Matrix client available for scheduling tool context")
            return None

        room = client.rooms.get(room_id)
        if room is None:
            self.logger.warning(
                "Skipping scheduler tool context because room is not cached",
                room_id=room_id,
            )
            return None

        return SchedulingToolContext(
            client=client,
            room=room,
            room_id=room_id,
            thread_id=self._resolve_reply_thread_id(thread_id, reply_to_event_id),
            requester_id=user_id or self.matrix_id.full_id,
            config=self.config,
        )

    def _build_openclaw_context(
        self,
        room_id: str,
        thread_id: str | None,
        user_id: str | None,
        *,
        agent_name: str | None = None,
    ) -> OpenClawToolContext | None:
        """Build runtime context for OpenClaw-compatible tool calls."""
        if self.client is None:
            return None
        return OpenClawToolContext(
            agent_name=agent_name or self.agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=user_id or self.matrix_id.full_id,
            client=self.client,
            config=self.config,
            storage_path=self.storage_path,
        )

    async def _generate_team_response_helper(
        self,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        message: str,
        team_agents: list[MatrixID],
        team_mode: str,
        thread_history: list[dict],
        requester_user_id: str,
        existing_event_id: str | None = None,
        images: Sequence[Image] | None = None,
    ) -> str | None:
        """Generate a team response (shared between preformed teams and TeamBot).

        Returns the initial message ID if created, None otherwise.
        """
        assert self.client is not None

        # Get the appropriate model for this team and room
        model_name = select_model_for_team(self.agent_name, room_id, self.config)

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
        agent_names = [mid.agent_name(self.config) or mid.username for mid in team_agents]
        scheduler_context = self._build_scheduling_tool_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=requester_user_id,
        )
        openclaw_context = self._build_openclaw_context(room_id, thread_id, requester_user_id)
        orchestrator = self.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)

        # Create async function for team response generation that takes message_id as parameter
        client = self.client

        async def generate_team_response(message_id: str | None) -> None:
            if use_streaming and not existing_event_id:
                # Show typing indicator while team generates streaming response
                async with typing_indicator(client, room_id):
                    with scheduling_tool_context(scheduler_context), openclaw_tool_context(openclaw_context):
                        response_stream = team_response_stream(
                            agent_ids=team_agents,
                            message=message,
                            orchestrator=orchestrator,
                            mode=mode,
                            thread_history=thread_history,
                            model_name=model_name,
                            images=images,
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
                            room_mode=self.thread_mode == "room",
                        )

                # Handle interactive questions in team responses
                await self._handle_interactive_question(
                    event_id,
                    accumulated,
                    room_id,
                    thread_id,
                    reply_to_event_id,
                    agent_name="team",
                )
            else:
                # Show typing indicator while team generates non-streaming response
                async with typing_indicator(client, room_id):
                    with scheduling_tool_context(scheduler_context), openclaw_tool_context(openclaw_context):
                        response_text = await team_response(
                            agent_names=agent_names,
                            mode=mode,
                            message=message,
                            orchestrator=orchestrator,
                            thread_history=thread_history,
                            model_name=model_name,
                            images=images,
                        )

                # Either edit the thinking message or send new
                if message_id:
                    await self._edit_message(room_id, message_id, response_text, thread_id)
                else:
                    assert self.client is not None
                    event_id = await self._send_response(
                        room_id,
                        reply_to_event_id,
                        response_text,
                        thread_id,
                    )
                    # Handle interactive questions in non-streaming team responses
                    if event_id:
                        await self._handle_interactive_question(
                            event_id,
                            response_text,
                            room_id,
                            thread_id,
                            reply_to_event_id,
                            agent_name="team",
                        )

        # Use unified handler for cancellation support
        # Always send thinking message unless we're editing an existing message
        thinking_msg = None
        if not existing_event_id:
            thinking_msg = "🤝 Team Response: Thinking..."

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
        response_function: object,  # Function that generates the response (takes message_id)
        thinking_message: str | None = None,  # None means don't send thinking message
        existing_event_id: str | None = None,
        user_id: str | None = None,  # User ID for presence check
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
            thinking_message: Thinking message to show (only used when existing_event_id is None)
            existing_event_id: ID of existing message to edit (for interactive questions)
            user_id: User ID for checking if they're online (for stop button decision)

        Returns:
            The initial message ID if created, None otherwise

        Note: In practice, either thinking_message or existing_event_id is provided, never both.

        """
        assert self.client is not None

        # Validate the mutual exclusivity constraint
        assert not (thinking_message and existing_event_id), (
            "thinking_message and existing_event_id are mutually exclusive"
        )

        # Send initial thinking message if not editing an existing message
        initial_message_id = None
        if thinking_message:
            assert not existing_event_id  # Redundant but makes the logic clear
            initial_message_id = await self._send_response(
                room_id,
                reply_to_event_id,
                f"{thinking_message} {IN_PROGRESS_MARKER}",
                thread_id,
            )

        # Determine which message ID to use
        message_id = existing_event_id or initial_message_id

        # Create cancellable task by calling the function with the message ID
        task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))  # type: ignore[operator]

        # Track for stop button (only if we have a message to track)
        message_to_track = existing_event_id or initial_message_id
        show_stop_button = False  # Default to not showing

        if message_to_track:
            self.stop_manager.set_current(message_to_track, room_id, task, None)

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
            self.logger.info("Response cancelled by user", message_id=message_to_track)
        except Exception as e:
            self.logger.exception("Error during response generation", error=str(e))
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
        images: Sequence[Image] | None = None,
    ) -> str | None:
        """Process a message and send a response (non-streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return None

        session_id = create_session_id(room_id, thread_id)
        knowledge = self._knowledge_for_agent(self.agent_name)
        scheduler_context = self._build_scheduling_tool_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
        )
        openclaw_context = self._build_openclaw_context(room_id, thread_id, user_id)
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                with scheduling_tool_context(scheduler_context), openclaw_tool_context(openclaw_context):
                    response_text = await ai_response(
                        agent_name=self.agent_name,
                        prompt=prompt,
                        session_id=session_id,
                        storage_path=self.storage_path,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        images=images,
                        reply_to_event_id=reply_to_event_id,
                        show_tool_calls=self.show_tool_calls,
                        tool_trace_collector=tool_trace,
                        run_metadata_collector=run_metadata_content,
                    )
        except asyncio.CancelledError:
            # Handle cancellation - send a message showing it was stopped
            self.logger.info("Non-streaming response cancelled by user", message_id=existing_event_id)
            if existing_event_id:
                cancelled_text = "**[Response cancelled by user]**"
                await self._edit_message(room_id, existing_event_id, cancelled_text, thread_id)
            raise
        except Exception as e:
            self.logger.exception("Error in non-streaming response", error=str(e))
            raise

        if existing_event_id:
            # Edit the existing message
            await self._edit_message(
                room_id,
                existing_event_id,
                response_text,
                thread_id,
                tool_trace=tool_trace if self.show_tool_calls else None,
                extra_content=run_metadata_content or None,
            )
            return existing_event_id

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(
            room_id,
            reply_to_event_id,
            response.formatted_text,
            thread_id,
            tool_trace=tool_trace if self.show_tool_calls else None,
            extra_content=run_metadata_content or None,
        )
        if event_id and response.option_map and response.options_list:
            # For interactive questions, use the same thread root that _send_response uses:
            # - If already in a thread, use that thread_id
            # - If not in a thread, use reply_to_event_id (the user's message) as thread root
            # This ensures consistency with how the bot creates threads
            thread_root_for_registration = self._resolve_reply_thread_id(thread_id, reply_to_event_id)
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
        scheduler_context = self._build_scheduling_tool_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
        )
        openclaw_context = self._build_openclaw_context(room_id, thread_id, user_id, agent_name=agent_name)
        show_tool_calls = self._show_tool_calls_for_agent(agent_name)
        tool_trace: list[ToolTraceEntry] = []
        run_metadata_content: dict[str, Any] = {}

        async with typing_indicator(self.client, room_id):
            with scheduling_tool_context(scheduler_context), openclaw_tool_context(openclaw_context):
                response_text = await ai_response(
                    agent_name=agent_name,
                    prompt=prompt,
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
            thread_root_for_registration = self._resolve_reply_thread_id(thread_id, reply_to_event_id)
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
            if self.config.get_agent_memory_backend(agent_name) != "file":
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
                thread_root_for_registration = self._resolve_reply_thread_id(thread_id, reply_to_event_id)
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
        images: Sequence[Image] | None = None,
    ) -> str | None:
        """Process a message and send a response (streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return None

        session_id = create_session_id(room_id, thread_id)
        knowledge = self._knowledge_for_agent(self.agent_name)
        scheduler_context = self._build_scheduling_tool_context(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            user_id=user_id,
        )
        openclaw_context = self._build_openclaw_context(room_id, thread_id, user_id)
        run_metadata_content: dict[str, Any] = {}

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                with scheduling_tool_context(scheduler_context), openclaw_tool_context(openclaw_context):
                    response_stream = stream_agent_response(
                        agent_name=self.agent_name,
                        prompt=prompt,
                        session_id=session_id,
                        storage_path=self.storage_path,
                        config=self.config,
                        thread_history=thread_history,
                        room_id=room_id,
                        knowledge=knowledge,
                        user_id=user_id,
                        images=images,
                        reply_to_event_id=reply_to_event_id,
                        show_tool_calls=self.show_tool_calls,
                        run_metadata_collector=run_metadata_content,
                    )

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
                        room_mode=self.thread_mode == "room",
                        show_tool_calls=self.show_tool_calls,
                        extra_content=run_metadata_content,
                    )

            # Handle interactive questions if present
            await self._handle_interactive_question(
                event_id,
                accumulated,
                room_id,
                thread_id,
                reply_to_event_id,
            )

        except asyncio.CancelledError:
            # send_streaming_response already preserves partial text and appends
            # a cancellation marker for the final edit.
            self.logger.info("Streaming cancelled by user", message_id=existing_event_id)
            raise
        except Exception as e:
            self.logger.exception("Error in streaming response", error=str(e))
            # Don't mark as responded if streaming failed
            return None
        else:
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
        images: Sequence[Image] | None = None,
    ) -> str | None:
        """Generate and send/edit a response using AI.

        Args:
            room_id: The room to send the response to
            prompt: The prompt to send to the AI
            reply_to_event_id: The event to reply to
            thread_id: Thread ID if in a thread
            thread_history: Thread history for context
            existing_event_id: If provided, edit this message instead of sending a new one
                             (only used for interactive question responses)
            user_id: User ID of the sender for identifying user messages in history
            images: Optional images to pass to the AI model

        Returns:
            Event ID of the response message, or None if failed

        """
        assert self.client is not None

        # Prepare session id for memory storage (store after sending response)
        session_id = create_session_id(room_id, thread_id)
        reprioritize_auto_flush_sessions(
            self.storage_path,
            self.config,
            agent_name=self.agent_name,
            active_session_id=session_id,
        )

        # Dynamically determine whether to use streaming based on user presence
        use_streaming = await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=user_id,
            enable_streaming=self.enable_streaming,
        )

        # Create async function for generation that takes message_id as parameter
        async def generate(message_id: str | None) -> None:
            if use_streaming:
                await self._process_and_respond_streaming(
                    room_id,
                    prompt,
                    reply_to_event_id,
                    thread_id,
                    thread_history,
                    message_id,  # Edit the thinking message or existing
                    user_id=user_id,
                    images=images,
                )
            else:
                await self._process_and_respond(
                    room_id,
                    prompt,
                    reply_to_event_id,
                    thread_id,
                    thread_history,
                    message_id,  # Edit the thinking message or existing
                    user_id=user_id,
                    images=images,
                )

        # Use unified handler for cancellation support
        # Always send "Thinking..." message unless we're editing an existing message
        thinking_msg = None
        if not existing_event_id:
            thinking_msg = "Thinking..."

        event_id = await self._run_cancellable_response(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            response_function=generate,
            thinking_message=thinking_msg,
            existing_event_id=existing_event_id,
            user_id=user_id,
        )

        # Store memory after response generation.
        try:
            mark_auto_flush_dirty_session(
                self.storage_path,
                self.config,
                agent_name=self.agent_name,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            )
            if self.config.get_agent_memory_backend(self.agent_name) != "file":
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

        Returns:
            Event ID if message was sent successfully, None otherwise.

        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        effective_thread_id = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            event_source=reply_to_event.source if reply_to_event else None,
        )

        if effective_thread_id is None:
            # Room mode: plain message, no thread metadata
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
            # Get the latest message in thread for MSC3440 fallback compatibility
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

        if self.thread_mode == "room":
            # Room mode: no thread metadata on edits
            content = format_message_with_mentions(
                self.config,
                new_text,
                sender_domain=sender_domain,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        else:
            # For edits in threads, we need to get the latest thread event ID for MSC3440 compliance
            # When editing, we still need the latest thread event for the fallback behavior
            # So we fetch it directly rather than using get_latest_thread_event_id_if_needed
            latest_thread_event_id = None
            if thread_id:
                assert self.client is not None
                # For edits, we always need the latest thread event ID
                # We can use the event being edited as the fallback if we can't get the latest
                latest_thread_event_id = await _latest_thread_event_id(self.client, room_id, thread_id)
                # If we couldn't get the latest, use the event being edited as fallback
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

    async def _handle_ai_routing(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText | nio.RoomMessageImage | nio.RoomEncryptedImage,
        thread_history: list[dict],
        thread_id: str | None = None,
        message: str | None = None,
        requester_user_id: str | None = None,
    ) -> None:
        # Only router agent should handle routing
        assert self.agent_name == ROUTER_AGENT_NAME

        # Use configured agents only - router should not suggest random agents
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
            # Send error message when routing fails
            response_text = "⚠️ I couldn't determine which agent should help with this. Please try mentioning an agent directly with @ or rephrase your request."
            self.logger.warning("Router failed to determine agent")
        else:
            # Router mentions the suggested agent and asks them to help
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = self.config.get_entity_thread_mode(suggested_agent) if suggested_agent else None
        thread_event_id = self._resolve_reply_thread_id(
            thread_id,
            event.event_id,
            event_source=event.source,
            thread_mode_override=target_thread_mode,
        )

        event_id = await self._send_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            response_text=response_text,
            thread_id=thread_event_id,
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
    ) -> None:
        """Handle an edited message by regenerating the agent's response.

        Args:
            room: The Matrix room
            event: The edited message event
            event_info: Information about the edit event

        """
        if not event_info.original_event_id:
            self.logger.debug("Edit event has no original event ID")
            return

        # Skip edits from other agents
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
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            sender_id=requester_user_id,
        )

        if not should_respond:
            self.logger.debug("Agent should not respond to edited message")
            return

        # These keys must be present according to MSC2676
        # https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/2676-message-editing.md
        edited_content = event.source["content"]["m.new_content"]["body"]

        # Remove the stale run from Agno history before regenerating.
        # The original run stored reply_to_event_id (= original_event_id) as
        # matrix_event_id in its metadata, so we look up by that key.
        session_id = create_session_id(room.room_id, context.thread_id)
        storage = create_session_storage(self.agent_name, self.storage_path)
        removed = remove_run_by_event_id(storage, session_id, event_info.original_event_id)
        if removed:
            self.logger.info("Removed stale run for edited message", event_id=event_info.original_event_id)

        # Generate new response
        await self._generate_response(
            room_id=room.room_id,
            prompt=edited_content,
            reply_to_event_id=event_info.original_event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            user_id=requester_user_id,
        )

        # Update the response tracker
        self.response_tracker.mark_responded(event_info.original_event_id, response_event_id)
        self.logger.info("Successfully regenerated response for edited message")

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:
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
        images: Sequence[Image] | None = None,
    ) -> None:
        """Generate a team response instead of individual agent response."""
        if not prompt.strip():
            return

        assert self.client is not None

        # Store memory once for the entire team (avoids duplicate LLM processing)
        session_id = create_session_id(room_id, thread_id)
        # Convert MatrixID list to agent names for memory storage
        agent_names = [mid.agent_name(self.config) or mid.username for mid in self.team_agents]
        create_background_task(
            store_conversation_memory(
                prompt,
                agent_names,  # Pass list of agent names for team storage
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

        # Use the shared team response helper
        await self._generate_team_response_helper(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            message=prompt,
            team_agents=self.team_agents,
            team_mode=self.team_mode,
            thread_history=thread_history,
            requester_user_id=user_id or "",
            existing_event_id=existing_event_id,
            images=images,
        )
