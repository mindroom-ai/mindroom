"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential

from . import config_confirmation, interactive, voice_handler
from .agents import create_agent, get_rooms_for_entity
from .ai import ai_response, stream_agent_response
from .background_tasks import create_background_task, wait_for_background_tasks
from .commands import (
    Command,
    CommandType,
    command_parser,
    get_command_help,
    handle_widget_command,
)
from .config import Config
from .config_commands import handle_config_command
from .constants import ENABLE_STREAMING, MATRIX_HOMESERVER, ROUTER_AGENT_NAME, VOICE_PREFIX
from .credentials_sync import sync_env_to_credentials
from .file_watcher import watch_file
from .logging_config import emoji, get_logger, setup_logging
from .matrix.client import (
    _latest_thread_event_id,
    check_and_set_avatar,
    edit_message,
    fetch_thread_history,
    get_joined_rooms,
    get_latest_thread_event_id_if_needed,
    get_room_members,
    invite_to_room,
    join_room,
    leave_room,
    send_message,
)
from .matrix.event_info import EventInfo
from .matrix.identity import (
    MatrixID,
    extract_agent_name,
    extract_server_name_from_homeserver,
)
from .matrix.mentions import format_message_with_mentions
from .matrix.presence import build_agent_status_message, is_user_online, set_presence_status, should_use_streaming
from .matrix.rooms import ensure_all_rooms_exist, ensure_user_in_rooms, is_dm_room, load_rooms, resolve_room_aliases
from .matrix.state import MatrixState
from .matrix.typing import typing_indicator
from .matrix.users import AgentMatrixUser, create_agent_user, login_agent_user
from .memory import store_conversation_memory
from .response_tracker import ResponseTracker
from .room_cleanup import cleanup_all_orphaned_bots
from .routing import suggest_agent_for_message
from .scheduling import (
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    list_scheduled_tasks,
    restore_scheduled_tasks,
    schedule_task,
)
from .stop import StopManager
from .streaming import (
    IN_PROGRESS_MARKER,
    ReplacementStreamingResponse,
    StreamingResponse,
    send_streaming_response,
)
from .teams import (
    TeamMode,
    decide_team_formation,
    select_model_for_team,
    team_response,
    team_response_stream,
)
from .thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_available_agents_in_room,
    get_configured_agents_for_room,
    has_user_responded_after_message,
    is_authorized_sender,
    should_agent_respond,
)

if TYPE_CHECKING:
    import structlog
    from agno.agent import Agent

logger = get_logger(__name__)


# Constants
SYNC_TIMEOUT_MS = 30000


def _create_task_wrapper(callback: object) -> object:
    """Create a wrapper that runs the callback as a background task.

    This ensures the sync loop is never blocked by event processing,
    allowing the bot to handle new events (like stop reactions) while
    processing messages.
    """

    async def wrapper(*args: object, **kwargs: object) -> None:
        # Create the task but don't await it - let it run in background
        async def error_handler() -> None:
            try:
                await callback(*args, **kwargs)  # type: ignore[operator]
            except asyncio.CancelledError:
                # Task was cancelled, this is expected during shutdown
                pass
            except Exception:
                # Log the exception with full traceback
                logger.exception("Error in event callback")

        # Create task with error handling
        _task = asyncio.create_task(error_handler())  # noqa: RUF006

    return wrapper


def _format_agent_description(agent_name: str, config: Config) -> str:
    """Format a concise agent description for the welcome message."""
    if agent_name in config.agents:
        agent_config = config.agents[agent_name]
        desc_parts = []

        # Add role first
        if agent_config.role:
            desc_parts.append(agent_config.role)

        # Add tools with better formatting
        if agent_config.tools:
            # Wrap each tool name in backticks
            formatted_tools = [f"`{tool}`" for tool in agent_config.tools[:3]]
            tools_str = ", ".join(formatted_tools)
            if len(agent_config.tools) > 3:
                tools_str += f" +{len(agent_config.tools) - 3} more"
            desc_parts.append(f"(🔧 {tools_str})")

        return " ".join(desc_parts) if desc_parts else ""

    if agent_name in config.teams:
        team_config = config.teams[agent_name]
        team_desc = f"Team of {len(team_config.agents)} agents"
        if team_config.role:
            return f"{team_config.role} ({team_desc})"
        return team_desc

    return ""


def _generate_welcome_message(room_id: str, config: Config) -> str:
    """Generate the welcome message text for a room."""
    # Get list of configured agents for this room
    configured_agents = get_configured_agents_for_room(room_id, config)

    # Build agent list for the welcome message
    agent_list = []
    for agent_id in configured_agents:
        agent_name = agent_id.agent_name(config)
        if not agent_name or agent_name == ROUTER_AGENT_NAME:
            continue

        description = _format_agent_description(agent_name, config)
        # Always show the agent, with or without description
        # Use the username with mindroom_ prefix (but without domain) for proper mention parsing
        agent_entry = f"• **@{agent_id.username}**"
        if description:
            agent_entry += f": {description}"
        agent_list.append(agent_entry)

    # Create welcome message
    welcome_msg = (
        "🎉 **Welcome to MindRoom!**\n\n"
        "I'm your routing assistant, here to help coordinate our team of specialized AI agents. 🤖\n\n"
    )

    if agent_list:
        welcome_msg += "🧠 **Available agents in this room:**\n"
        welcome_msg += "\n".join(agent_list)
        welcome_msg += "\n\n"

    welcome_msg += (
        "💬 **How to interact:**\n"
        "• Mention an agent with @ to get their attention (e.g., @mindroom_assistant)\n"
        "• Use `!help` to see available commands\n"
        "• Agents respond in threads to keep conversations organized\n"
        "• Multiple agents can collaborate when you mention them together\n"
        "• 🎤 Voice messages are automatically transcribed and work perfectly!\n\n"
        "⚡ **Quick commands:**\n"
        "• `!hi` - Show this welcome message again\n"
        "• `!widget` - Add configuration widget to this room\n"
        "• `!schedule <time> <message>` - Schedule tasks and reminders\n"
        "• `!help [topic]` - Get detailed help\n\n"
        "✨ Feel free to ask any agent for help or start a conversation!"
    )

    return welcome_msg


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
    enable_streaming = ENABLE_STREAMING

    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        # Convert agent names to MatrixID objects
        team_matrix_ids = [MatrixID.from_username(agent_name, config.domain) for agent_name in team_config.agents]
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            rooms=rooms,
            team_agents=team_matrix_ids,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=True,
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
    orchestrator: MultiAgentOrchestrator = field(init=False)  # Reference to orchestrator

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

    @property  # Not cached_property because Team mutates it!
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        return create_agent(agent_name=self.agent_name, config=self.config)

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

        # Leave rooms we're no longer configured for
        for room_id in current_rooms - configured_rooms:
            if await is_dm_room(self.client, room_id):
                self.logger.debug(f"Preserving DM room {room_id} during cleanup")
                continue
            success = await leave_room(self.client, room_id)
            if success:
                self.logger.info(f"Left unconfigured room {room_id}")
            else:
                self.logger.error(f"Failed to leave unconfigured room {room_id}")

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
        self.client.add_event_callback(_create_task_wrapper(self._on_invite), nio.InviteEvent)
        self.client.add_event_callback(_create_task_wrapper(self._on_message), nio.RoomMessageText)
        self.client.add_event_callback(_create_task_wrapper(self._on_reaction), nio.ReactionEvent)

        # Register voice message callbacks (only for router agent to avoid duplicates)
        if self.agent_name == ROUTER_AGENT_NAME:
            self.client.add_event_callback(_create_task_wrapper(self._on_voice_message), nio.RoomMessageAudio)
            self.client.add_event_callback(_create_task_wrapper(self._on_voice_message), nio.RoomEncryptedAudio)

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
        # Leave all rooms
        try:
            joined_rooms = await get_joined_rooms(self.client)
            if joined_rooms:
                for room_id in joined_rooms:
                    if await is_dm_room(self.client, room_id):
                        self.logger.debug(f"Preserving DM room {room_id} during cleanup")
                        continue

                    success = await leave_room(self.client, room_id)
                    if success:
                        self.logger.info(f"Left room {room_id} during cleanup")
                    else:
                        self.logger.error(f"Failed to leave room {room_id} during cleanup")
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

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:  # noqa: C901, PLR0911, PLR0912
        self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
        assert self.client is not None
        if event.body.endswith(IN_PROGRESS_MARKER):
            return

        # Skip our own messages (unless voice transcription from router)
        if event.sender == self.matrix_id.full_id and not event.body.startswith(VOICE_PREFIX):
            return

        event_info = EventInfo.from_event(event.source)

        # Check if sender is authorized to interact with agents
        is_authorized = is_authorized_sender(event.sender, self.config, room.room_id)
        self.logger.debug(
            f"Authorization check for {event.sender}: authorized={is_authorized}, room={room.room_id}",
        )
        if not is_authorized:
            # Mark as seen even though we're not responding (prevents reprocessing after permission changes)
            # Only mark non-edit events as responded
            if not event_info.is_edit:
                self.response_tracker.mark_responded(event.event_id)
            self.logger.debug(f"Ignoring message from unauthorized sender: {event.sender}")
            return

        # Handle edit events
        if event_info.is_edit:
            await self._handle_message_edit(room, event, event_info)
            return

        # Check if we've already seen this message (prevents reprocessing after restart)
        if self.response_tracker.has_responded(event.event_id):
            self.logger.debug("Already seen message", event_id=event.event_id)
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
        sender_agent_name = extract_agent_name(event.sender, self.config)

        # Skip messages from agents unless:
        # 1. We're mentioned
        # 2. It's a voice transcription from router (treated as user message)
        is_router_voice = sender_agent_name == ROUTER_AGENT_NAME and event.body.startswith(VOICE_PREFIX)
        if sender_agent_name and not context.am_i_mentioned and not is_router_voice:
            self.logger.debug("Ignoring message from other agent (not mentioned)")
            return

        # Get agents in thread (excludes router)
        agents_in_thread = get_agents_in_thread(context.thread_history, self.config)

        # Router: Route when no specific agent mentioned and no agents in thread
        if self.agent_name == ROUTER_AGENT_NAME:
            # Only perform AI routing when:
            # 1. No specific agent is mentioned
            # 2. No agents are already in the thread
            # 3. There's more than one agent available (routing makes sense)
            if not context.mentioned_agents and not agents_in_thread:
                available_agents = get_available_agents_in_room(room, self.config)
                if len(available_agents) == 1:
                    # Skip routing in single-agent rooms - the agent will handle it directly
                    self.logger.info("Skipping routing: only one agent present")
                else:
                    # Multiple agents available - perform AI routing
                    await self._handle_ai_routing(room, event, context.thread_history)
            # Router's job is done after routing/command handling/voice transcription
            return

        # Check for team formation
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(context.thread_history, self.config)
        form_team = await decide_team_formation(
            self.matrix_id,
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            room=room,
            message=event.body,
            config=self.config,
            is_dm_room=_is_dm_room,
            is_thread=context.is_thread,
        )

        # Handle team formation (only first agent alphabetically)
        if form_team.should_form_team and self.matrix_id in form_team.agents:
            # Determine if this agent should lead the team response
            # Use the same ordering as decide_team_formation (by full_id)
            first_agent = min(form_team.agents, key=lambda x: x.full_id)
            if self.matrix_id != first_agent:
                return

            # Use the shared team response helper
            response_event_id = await self._generate_team_response_helper(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=context.thread_id,
                message=event.body,
                team_agents=form_team.agents,
                team_mode=form_team.mode,
                thread_history=context.thread_history,
                requester_user_id=event.sender,
                existing_event_id=None,
            )

            self.response_tracker.mark_responded(event.event_id, response_event_id)
            return

        # Check if we should respond individually
        should_respond = should_agent_respond(
            agent_name=self.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room=room,
            thread_history=context.thread_history,
            config=self.config,
            mentioned_agents=context.mentioned_agents,
        )

        if not should_respond:
            return

        # Log if responding without mention
        if not context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        # Generate and send response
        self.logger.info("Processing", event_id=event.event_id)
        response_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=event.body,
            reply_to_event_id=event.event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            user_id=event.sender,
        )
        self.response_tracker.mark_responded(event.event_id, response_event_id)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
        assert self.client is not None

        # Check if sender is authorized to interact with agents
        if not is_authorized_sender(event.sender, self.config, room.room_id):
            self.logger.debug(f"Ignoring reaction from unauthorized sender: {event.sender}")
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

        # Otherwise handle as interactive question
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
        # Only process if voice handler is enabled
        if not self.config.voice.enabled:
            return

        # Don't process our own voice messages
        if event.sender == self.matrix_id.full_id:
            return

        # Check if we've already seen this voice message (prevents reprocessing after restart)
        if self.response_tracker.has_responded(event.event_id):
            self.logger.debug("Already processed voice message", event_id=event.event_id)
            return

        # Check if sender is authorized to interact with agents
        if not is_authorized_sender(event.sender, self.config, room.room_id):
            # Mark as seen even though we're not responding
            self.response_tracker.mark_responded(event.event_id)
            self.logger.debug(f"Ignoring voice message from unauthorized sender: {event.sender}")
            return

        self.logger.info("Processing voice message", event_id=event.event_id, sender=event.sender)

        transcribed_message = await voice_handler.handle_voice_message(self.client, room, event, self.config)

        if transcribed_message:
            event_info = EventInfo.from_event(event.source)
            response_event_id = await self._send_response(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text=transcribed_message,
                thread_id=event_info.thread_id,
            )
            self.response_tracker.mark_responded(event.event_id, response_event_id)
        else:
            # Mark as responded to avoid reprocessing
            self.response_tracker.mark_responded(event.event_id)

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> MessageContext:
        assert self.client is not None

        # Check if mentions should be ignored for this message
        skip_mentions = _should_skip_mentions(event.source)

        if skip_mentions:
            # Don't detect mentions if the message has skip_mentions metadata
            mentioned_agents: list[MatrixID] = []
            am_i_mentioned = False
        else:
            mentioned_agents, am_i_mentioned = check_agent_mentioned(event.source, self.matrix_id, self.config)

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id, room_name=room.name)

        event_info = EventInfo.from_event(event.source)

        thread_history = []
        if event_info.thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, event_info.thread_id)

        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=event_info.is_thread,
            thread_id=event_info.thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
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
    ) -> str | None:
        """Generate a team response (shared between preformed teams and TeamBot).

        Returns the initial message ID if created, None otherwise.
        """
        assert self.client is not None

        # Get the appropriate model for this team and room
        model_name = select_model_for_team(self.agent_name, room_id, self.config)

        # Decide streaming based on presence
        use_streaming = self.enable_streaming and await should_use_streaming(
            self.client,
            room_id,
            requester_user_id=requester_user_id,
        )

        # Convert mode string to TeamMode enum
        mode = TeamMode.COORDINATE if team_mode == "coordinate" else TeamMode.COLLABORATE

        # Convert MatrixID list to agent names for non-streaming APIs
        agent_names = [mid.agent_name(self.config) or mid.username for mid in team_agents]

        # Create async function for team response generation that takes message_id as parameter
        async def generate_team_response(message_id: str | None) -> None:
            if use_streaming and not existing_event_id:
                # Show typing indicator while team generates streaming response
                async with typing_indicator(self.client, room_id):
                    response_stream = team_response_stream(
                        agent_ids=team_agents,
                        message=message,
                        orchestrator=self.orchestrator,
                        mode=mode,
                        thread_history=thread_history,
                        model_name=model_name,
                    )

                    event_id, accumulated = await send_streaming_response(
                        self.client,
                        room_id,
                        reply_to_event_id,
                        thread_id,
                        self.matrix_id.domain,
                        self.config,
                        response_stream,
                        streaming_cls=ReplacementStreamingResponse,
                        header=None,
                        existing_event_id=message_id,
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
                async with typing_indicator(self.client, room_id):
                    response_text = await team_response(
                        agent_names=agent_names,
                        mode=mode,
                        message=message,
                        orchestrator=self.orchestrator,
                        thread_history=thread_history,
                        model_name=model_name,
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
    ) -> str | None:
        """Process a message and send a response (non-streaming)."""
        if not prompt.strip():
            return None

        session_id = create_session_id(room_id, thread_id)

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                response_text = await ai_response(
                    agent_name=self.agent_name,
                    prompt=prompt,
                    session_id=session_id,
                    storage_path=self.storage_path,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
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
            await self._edit_message(room_id, existing_event_id, response_text, thread_id)
            return existing_event_id

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(room_id, reply_to_event_id, response.formatted_text, thread_id)
        if event_id and response.option_map and response.options_list:
            # For interactive questions, use the same thread root that _send_response uses:
            # - If already in a thread, use that thread_id
            # - If not in a thread, use reply_to_event_id (the user's message) as thread root
            # This ensures consistency with how the bot creates threads
            thread_root_for_registration = thread_id if thread_id else reply_to_event_id
            interactive.register_interactive_question(
                event_id,
                room_id,
                thread_root_for_registration,
                response.option_map,
                self.agent_name,
            )
            await interactive.add_reaction_buttons(self.client, room_id, event_id, response.options_list)

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
                thread_root_for_registration = thread_id if thread_id else reply_to_event_id
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
    ) -> str | None:
        """Process a message and send a response (streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return None

        session_id = create_session_id(room_id, thread_id)

        try:
            # Show typing indicator while generating response
            async with typing_indicator(self.client, room_id):
                response_stream = stream_agent_response(
                    agent_name=self.agent_name,
                    prompt=prompt,
                    session_id=session_id,
                    storage_path=self.storage_path,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
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
            # Handle cancellation - send a message showing it was stopped
            self.logger.info("Streaming cancelled by user", message_id=existing_event_id)
            if existing_event_id:
                cancelled_text = "**[Response cancelled by user]**"
                await self._edit_message(room_id, existing_event_id, cancelled_text, thread_id)
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

        Returns:
            Event ID of the response message, or None if failed

        """
        assert self.client is not None

        # Prepare session id for memory storage (store after sending response)
        session_id = create_session_id(room_id, thread_id)

        # Dynamically determine whether to use streaming based on user presence
        # Only check presence if streaming is globally enabled
        use_streaming = self.enable_streaming
        if use_streaming:
            # Check if the user is online to decide whether to stream
            use_streaming = await should_use_streaming(self.client, room_id, requester_user_id=user_id)

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
                )
            else:
                await self._process_and_respond(
                    room_id,
                    prompt,
                    reply_to_event_id,
                    thread_id,
                    thread_history,
                    message_id,  # Edit the thinking message or existing
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

        # Store memory after response generation; ignore errors in tests/mocks
        # TODO: Remove try-except and fix tests
        try:
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
        except Exception:  # pragma: no cover
            self.logger.debug("Skipping memory storage due to configuration error")

        return event_id

    async def _send_response(
        self,
        room_id: str,
        reply_to_event_id: str | None,
        response_text: str,
        thread_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
        skip_mentions: bool = False,
    ) -> str | None:
        """Send a response message to a room.

        Args:
            room_id: The room id to send to
            reply_to_event_id: The event ID to reply to (can be None when in a thread)
            response_text: The text to send
            thread_id: The thread ID if already in a thread
            reply_to_event: Optional event object for the message we're replying to (used to check for safe thread root)
            skip_mentions: If True, add metadata to indicate mentions should not trigger responses

        Returns:
            Event ID if message was sent successfully, None otherwise.

        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        # Always ensure we have a thread_id - use the original message as thread root if needed
        # This ensures agents always respond in threads, even when mentioned in main room
        event_info = EventInfo.from_event(reply_to_event.source if reply_to_event else None)
        effective_thread_id = thread_id or event_info.safe_thread_root or reply_to_event_id

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

    async def _edit_message(self, room_id: str, event_id: str, new_text: str, thread_id: str | None) -> bool:
        """Edit an existing message.

        Returns:
            True if edit was successful, False otherwise.

        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

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
        event: nio.RoomMessageText,
        thread_history: list[dict],
    ) -> None:
        # Only router agent should handle routing
        assert self.agent_name == ROUTER_AGENT_NAME

        # Use configured agents only - router should not suggest random agents
        available_agents = get_configured_agents_for_room(room.room_id, self.config)
        if not available_agents:
            self.logger.debug("No configured agents to route to in this room")
            return

        self.logger.info("Handling AI routing", event_id=event.event_id)

        event_info = EventInfo.from_event(event.source)
        suggested_agent = await suggest_agent_for_message(
            event.body,
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
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        # If no thread exists, create one with the original message as root
        thread_event_id = event_info.thread_id
        if not thread_event_id:
            # Check if the current event can be a thread root
            thread_event_id = event_info.safe_thread_root or event.event_id

        # Get latest thread event for MSC3440 compliance when no specific reply
        # Note: We use event.event_id as reply_to for routing suggestions
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            self.client,
            room.room_id,
            thread_event_id,
            event.event_id,
        )

        content = format_message_with_mentions(
            self.config,
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_event_id,
            reply_to_event_id=event.event_id,
            latest_thread_event_id=latest_thread_event_id,
        )

        assert self.client is not None
        event_id = await send_message(self.client, room.room_id, content)
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
        )

        if not should_respond:
            self.logger.debug("Agent should not respond to edited message")
            return

        # These keys must be present according to MSC2676
        # https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/2676-message-editing.md
        edited_content = event.source["content"]["m.new_content"]["body"]

        # Generate new response
        await self._generate_response(
            room_id=room.room_id,
            prompt=edited_content,
            reply_to_event_id=event_info.original_event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            user_id=event.sender,
        )

        # Update the response tracker
        self.response_tracker.mark_responded(event_info.original_event_id, response_event_id)
        self.logger.info("Successfully regenerated response for edited message")

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:  # noqa: C901, PLR0912
        self.logger.info("Handling command", command_type=command.type.value)

        event_info = EventInfo.from_event(event.source)

        # Widget command modifies room state, so it doesn't need a thread
        if command.type == CommandType.WIDGET:
            assert self.client is not None
            url = command.args.get("url")
            response_text = await handle_widget_command(client=self.client, room_id=room.room_id, url=url)
            # Send response in thread if in thread, otherwise in main room
            await self._send_response(room.room_id, event.event_id, response_text, event_info.thread_id)
            return

        # For commands that need thread context, use the existing thread or the event will start a new one
        # The _send_response method will automatically create a thread if needed
        effective_thread_id = event_info.thread_id or event_info.safe_thread_root or event.event_id

        response_text = ""

        if command.type == CommandType.HELP:
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        elif command.type == CommandType.HI:
            # Generate the welcome message for this room
            response_text = _generate_welcome_message(room.room_id, self.config)

        elif command.type == CommandType.SCHEDULE:
            full_text = command.args["full_text"]

            # Get mentioned agents from the command text
            mentioned_agents, _ = check_agent_mentioned(event.source, None, self.config)

            assert self.client is not None
            task_id, response_text = await schedule_task(
                client=self.client,
                room_id=room.room_id,
                thread_id=effective_thread_id,
                scheduled_by=event.sender,
                full_text=full_text,
                config=self.config,
                room=room,
                mentioned_agents=mentioned_agents,
            )

        elif command.type == CommandType.LIST_SCHEDULES:
            assert self.client is not None
            response_text = await list_scheduled_tasks(
                client=self.client,
                room_id=room.room_id,
                thread_id=effective_thread_id,
                config=self.config,
            )

        elif command.type == CommandType.CANCEL_SCHEDULE:
            assert self.client is not None
            cancel_all = command.args.get("cancel_all", False)

            if cancel_all:
                # Cancel all scheduled tasks
                response_text = await cancel_all_scheduled_tasks(
                    client=self.client,
                    room_id=room.room_id,
                )
            else:
                # Cancel specific task
                task_id = command.args["task_id"]
                response_text = await cancel_scheduled_task(
                    client=self.client,
                    room_id=room.room_id,
                    task_id=task_id,
                )

        elif command.type == CommandType.CONFIG:
            # Handle config command
            args_text = command.args.get("args_text", "")
            response_text, change_info = await handle_config_command(args_text)

            # If we have change_info, this is a config set that needs confirmation
            if change_info:
                # Send the preview message
                event_id = await self._send_response(
                    room.room_id,
                    event.event_id,
                    response_text,
                    event_info.thread_id,
                    reply_to_event=event,
                    skip_mentions=True,
                )

                if event_id:
                    # Register the pending change
                    config_confirmation.register_pending_change(
                        event_id=event_id,
                        room_id=room.room_id,
                        thread_id=event_info.thread_id,
                        config_path=change_info["config_path"],
                        old_value=change_info["old_value"],
                        new_value=change_info["new_value"],
                        requester=event.sender,
                    )

                    # Get the pending change we just registered
                    pending_change = config_confirmation.get_pending_change(event_id)

                    # Store in Matrix state for persistence
                    if pending_change:
                        await config_confirmation.store_pending_change_in_matrix(
                            self.client,
                            event_id,
                            pending_change,
                        )

                    # Add reaction buttons
                    await config_confirmation.add_confirmation_reactions(self.client, room.room_id, event_id)

                self.response_tracker.mark_responded(event.event_id)
                return  # Exit early since we've handled the response

        elif command.type == CommandType.UNKNOWN:
            # Handle unknown commands
            response_text = "❌ Unknown command. Try !help for available commands."

        if response_text:
            await self._send_response(
                room.room_id,
                event.event_id,
                response_text,
                event_info.thread_id,
                reply_to_event=event,
                skip_mentions=True,
            )
            self.response_tracker.mark_responded(event.event_id)


@dataclass
class TeamBot(AgentBot):
    """A bot that represents a team of agents working together."""

    team_agents: list[MatrixID] = field(default_factory=list)
    team_mode: str = field(default="coordinate")
    team_model: str | None = field(default=None)

    @cached_property
    def agent(self) -> Agent | None:  # type: ignore[override]
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
        )


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _sync_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)

    async def _ensure_user_account(self) -> None:
        """Ensure a user account exists, creating one if necessary.

        This reuses the same create_agent_user function that agents use,
        treating the user as a special "agent" named "user".
        """
        # The user account is just another "agent" from the perspective of account management
        user_account = await create_agent_user(
            MATRIX_HOMESERVER,
            "user",  # Special agent name for the human user
            "Mindroom User",  # Display name
        )
        logger.info(f"User account ready: {user_account.user_id}")

    async def initialize(self) -> None:
        """Initialize all agent bots with self-management.

        Each agent is now responsible for ensuring its own user account and rooms.
        """
        logger.info("Initializing multi-agent system...")

        # Ensure user account exists first
        await self._ensure_user_account()

        config = Config.from_yaml()
        self.config = config

        # Create bots for all configured entities
        # Make Router the first so that it can manage room invitations
        all_entities = [ROUTER_AGENT_NAME, *list(config.agents.keys()), *list(config.teams.keys())]

        for entity_name in all_entities:
            # Create a temporary agent user object (will be updated by ensure_user_account)
            if entity_name == ROUTER_AGENT_NAME:
                temp_user = AgentMatrixUser(
                    agent_name=ROUTER_AGENT_NAME,
                    user_id="",  # Will be set by ensure_user_account
                    display_name="RouterAgent",
                    password="",  # Will be set by ensure_user_account
                )
            elif entity_name in config.agents:
                temp_user = AgentMatrixUser(
                    agent_name=entity_name,
                    user_id="",
                    display_name=config.agents[entity_name].display_name,
                    password="",
                )
            elif entity_name in config.teams:
                temp_user = AgentMatrixUser(
                    agent_name=entity_name,
                    user_id="",
                    display_name=config.teams[entity_name].display_name,
                    password="",
                )
            else:
                continue

            bot = create_bot_for_entity(entity_name, temp_user, config, self.storage_path)
            if bot is None:
                logger.warning(f"Could not create bot for {entity_name}")
                continue

            bot.orchestrator = self
            self.agent_bots[entity_name] = bot

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots."""
        if not self.agent_bots:
            await self.initialize()

        # Start each agent bot (this registers callbacks and logs in, but doesn't join rooms)
        start_tasks = [bot.try_start() for bot in self.agent_bots.values()]
        results = await asyncio.gather(*start_tasks)

        # Check for failures
        failed_agents = [bot.agent_name for bot, success in zip(self.agent_bots.values(), results) if not success]

        if len(failed_agents) == len(self.agent_bots):
            msg = "All agents failed to start - cannot proceed"
            raise RuntimeError(msg)
        if failed_agents:
            logger.warning(
                f"System starting in degraded mode. "
                f"Failed agents: {', '.join(failed_agents)} "
                f"({len(self.agent_bots) - len(failed_agents)}/{len(self.agent_bots)} operational)",
            )
        else:
            logger.info("All agent bots started successfully")

        self.running = True

        # Setup rooms and have all bots join them
        await self._setup_rooms_and_memberships(list(self.agent_bots.values()))

        # Create sync tasks for each bot with automatic restart on failure
        for entity_name, bot in self.agent_bots.items():
            # Create a task for each bot's sync loop with restart wrapper
            sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
            # Store the task reference for later cancellation
            self._sync_tasks[entity_name] = sync_task

        # Run all sync tasks
        await asyncio.gather(*tuple(self._sync_tasks.values()))

    async def update_config(self) -> bool:  # noqa: C901, PLR0912
        """Update configuration with simplified self-managing agents.

        Each agent handles its own user account creation and room management.

        Returns:
            True if any agents were updated, False otherwise.

        """
        new_config = Config.from_yaml()

        if not self.config:
            self.config = new_config
            return False

        # Identify what changed - we can keep using the existing helper functions
        entities_to_restart = await _identify_entities_to_restart(self.config, new_config, self.agent_bots)

        # Also check for new entities that didn't exist before
        all_new_entities = set(new_config.agents.keys()) | set(new_config.teams.keys()) | {ROUTER_AGENT_NAME}
        existing_entities = set(self.agent_bots.keys())
        new_entities = all_new_entities - existing_entities

        # Always update the orchestrator's config first
        self.config = new_config

        # Always update config for ALL existing bots (even those being restarted will get new config when recreated)
        logger.info(
            f"Updating config. New authorization: {new_config.authorization.global_users}",
        )
        for entity_name, bot in self.agent_bots.items():
            if entity_name not in entities_to_restart:
                bot.config = new_config
                await bot._set_presence_with_model_info()
                logger.debug(f"Updated config for {entity_name}")

        if not entities_to_restart and not new_entities:
            # No entities to restart or create, we're done
            return False

        # Stop entities that need restarting
        if entities_to_restart:
            await _stop_entities(entities_to_restart, self.agent_bots, self._sync_tasks)

        # Recreate entities that need restarting using self-management
        for entity_name in entities_to_restart:
            if entity_name in all_new_entities:
                # Create temporary user object (will be updated by ensure_user_account)
                temp_user = _create_temp_user(entity_name, new_config)
                bot = create_bot_for_entity(entity_name, temp_user, new_config, self.storage_path)  # type: ignore[assignment]
                if bot:
                    bot.orchestrator = self
                    self.agent_bots[entity_name] = bot
                    # Agent handles its own setup (but doesn't join rooms yet)
                    if await bot.try_start():
                        # Start sync loop with automatic restart
                        sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
                        self._sync_tasks[entity_name] = sync_task
                    else:
                        # Remove the failed bot from our registry
                        del self.agent_bots[entity_name]
            # Entity was removed from config
            elif entity_name in self.agent_bots:
                del self.agent_bots[entity_name]

        # Create new entities
        for entity_name in new_entities:
            temp_user = _create_temp_user(entity_name, new_config)
            bot = create_bot_for_entity(entity_name, temp_user, new_config, self.storage_path)  # type: ignore[assignment]
            if bot:
                bot.orchestrator = self
                self.agent_bots[entity_name] = bot
                if await bot.try_start():
                    sync_task = asyncio.create_task(_sync_forever_with_restart(bot))
                    self._sync_tasks[entity_name] = sync_task
                else:
                    # Remove the failed bot from our registry
                    del self.agent_bots[entity_name]

        # Handle removed entities (cleanup)
        removed_entities = existing_entities - all_new_entities
        for entity_name in removed_entities:
            # Cancel sync task first
            await _cancel_sync_task(entity_name, self._sync_tasks)

            if entity_name in self.agent_bots:
                bot = self.agent_bots[entity_name]
                await bot.cleanup()  # Agent handles its own cleanup
                del self.agent_bots[entity_name]

        # Setup rooms and have new/restarted bots join them
        bots_to_setup = [
            self.agent_bots[entity_name]
            for entity_name in entities_to_restart | new_entities
            if entity_name in self.agent_bots
        ]

        if bots_to_setup:
            await self._setup_rooms_and_memberships(bots_to_setup)

        logger.info(f"Configuration update complete: {len(entities_to_restart) + len(new_entities)} bots affected")
        return True

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False

        # First cancel all sync tasks
        for entity_name in list(self._sync_tasks.keys()):
            await _cancel_sync_task(entity_name, self._sync_tasks)

        # Signal all bots to stop their sync loops
        for bot in self.agent_bots.values():
            bot.running = False

        # Now stop all bots
        stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")

    async def _setup_rooms_and_memberships(self, bots: list[AgentBot | TeamBot]) -> None:
        """Setup rooms and ensure all bots have correct memberships.

        This shared method handles the common room setup flow for both
        initial startup and configuration updates.

        Args:
            bots: Collection of bots to setup room memberships for

        """
        # Ensure all configured rooms exist (router creates them if needed)
        await self._ensure_rooms_exist()

        # After rooms exist, update each bot's room list to use room IDs instead of aliases
        assert self.config is not None
        for bot in bots:
            # Get the room aliases for this entity from config and resolve to IDs
            room_aliases = get_rooms_for_entity(bot.agent_name, self.config)
            bot.rooms = resolve_room_aliases(room_aliases)

        # After rooms exist, ensure room invitations are up to date
        await self._ensure_room_invitations()

        # Ensure user joins all rooms after being invited
        # Get all room IDs (not just newly created ones)
        all_rooms = load_rooms()
        all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
        if all_room_ids:
            await ensure_user_in_rooms(MATRIX_HOMESERVER, all_room_ids)

        # Now have bots join their configured rooms
        join_tasks = [bot.ensure_rooms() for bot in bots]
        await asyncio.gather(*join_tasks)
        logger.info("All agents have joined their configured rooms")

    async def _ensure_rooms_exist(self) -> None:
        """Ensure all configured rooms exist, creating them if necessary.

        This uses the router bot's client to create rooms since it has the necessary permissions.
        """
        if ROUTER_AGENT_NAME not in self.agent_bots:
            logger.warning("Router not available, cannot ensure rooms exist")
            return

        router_bot = self.agent_bots[ROUTER_AGENT_NAME]
        if router_bot.client is None:
            logger.warning("Router client not available, cannot ensure rooms exist")
            return

        # Directly create rooms using the router's client
        assert self.config is not None
        room_ids = await ensure_all_rooms_exist(router_bot.client, self.config)
        logger.info(f"Ensured existence of {len(room_ids)} rooms")

    async def _ensure_room_invitations(self) -> None:  # noqa: C901, PLR0912
        """Ensure all agents and the user are invited to their configured rooms.

        This uses the router bot's client to manage room invitations,
        as the router has admin privileges in all rooms.
        """
        if ROUTER_AGENT_NAME not in self.agent_bots:
            logger.warning("Router not available, cannot ensure room invitations")
            return

        router_bot = self.agent_bots[ROUTER_AGENT_NAME]
        if router_bot.client is None:
            logger.warning("Router client not available, cannot ensure room invitations")
            return

        # Get the current configuration
        config = self.config
        if not config:
            logger.warning("No configuration available, cannot ensure room invitations")
            return

        # Get all rooms the router is in
        joined_rooms = await get_joined_rooms(router_bot.client)
        if not joined_rooms:
            return

        server_name = extract_server_name_from_homeserver(MATRIX_HOMESERVER)

        # First, invite the user account to all rooms
        state = MatrixState.load()
        user_account = state.get_account("agent_user")  # User is stored as "agent_user"
        if user_account:
            user_id = MatrixID.from_username(user_account.username, server_name).full_id
            for room_id in joined_rooms:
                room_members = await get_room_members(router_bot.client, room_id)
                if user_id not in room_members:
                    success = await invite_to_room(router_bot.client, room_id, user_id)
                    if success:
                        logger.info(f"Invited user {user_id} to room {room_id}")
                    else:
                        logger.warning(f"Failed to invite user {user_id} to room {room_id}")

        for room_id in joined_rooms:
            # Get who should be in this room based on configuration
            configured_bots = config.get_configured_bots_for_room(room_id)

            if not configured_bots:
                continue

            # Get current members of the room
            current_members = await get_room_members(router_bot.client, room_id)

            # Invite missing bots
            for bot_username in configured_bots:
                bot_user_id = MatrixID.from_username(bot_username, server_name).full_id

                if bot_user_id not in current_members:
                    # Bot should be in room but isn't - invite them
                    success = await invite_to_room(router_bot.client, room_id, bot_user_id)
                    if success:
                        logger.info(f"Invited {bot_username} to room {room_id}")
                    else:
                        logger.warning(f"Failed to invite {bot_username} to room {room_id}")

        logger.info("Ensured room invitations for all configured agents")


async def _identify_entities_to_restart(
    config: Config | None,
    new_config: Config,
    agent_bots: dict[str, Any],
) -> set[str]:
    """Identify entities that need restarting due to config changes."""
    agents_to_restart = _get_changed_agents(config, new_config, agent_bots)
    teams_to_restart = _get_changed_teams(config, new_config, agent_bots)

    entities_to_restart = agents_to_restart | teams_to_restart

    if _router_needs_restart(config, new_config):
        entities_to_restart.add(ROUTER_AGENT_NAME)

    return entities_to_restart


def _get_changed_agents(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
    if not config:
        return set()

    changed = set()
    all_agents = set(config.agents.keys()) | set(new_config.agents.keys())

    for agent_name in all_agents:
        old_agent = config.agents.get(agent_name)
        new_agent = new_config.agents.get(agent_name)

        # Compare agents using model_dump with exclude_none=True to match how configs are saved
        # This prevents false positives when None values are involved
        if old_agent and new_agent:
            # Both exist - compare their non-None values (matching save_to_yaml behavior)
            old_dict = old_agent.model_dump(exclude_none=True)
            new_dict = new_agent.model_dump(exclude_none=True)
            agents_differ = old_dict != new_dict
        else:
            # One is None - they definitely differ
            agents_differ = old_agent != new_agent

        # Only restart if this specific agent's configuration has changed
        # (not just global config changes like authorization)
        if agents_differ and (agent_name in agent_bots or new_agent is not None):
            if old_agent and new_agent:
                logger.debug(f"Agent {agent_name} configuration changed, will restart")
            elif new_agent:
                logger.info(f"Agent {agent_name} is new, will start")
            else:
                logger.info(f"Agent {agent_name} was removed, will stop")
            changed.add(agent_name)

    return changed


def _get_changed_teams(config: Config | None, new_config: Config, agent_bots: dict[str, Any]) -> set[str]:
    if not config:
        return set()

    changed = set()
    all_teams = set(config.teams.keys()) | set(new_config.teams.keys())

    for team_name in all_teams:
        old_team = config.teams.get(team_name)
        new_team = new_config.teams.get(team_name)

        # Compare teams using model_dump with exclude_none=True to match how configs are saved
        if old_team and new_team:
            old_dict = old_team.model_dump(exclude_none=True)
            new_dict = new_team.model_dump(exclude_none=True)
            teams_differ = old_dict != new_dict
        else:
            teams_differ = old_team != new_team

        if teams_differ and (team_name in agent_bots or new_team is not None):
            changed.add(team_name)

    return changed


def _router_needs_restart(config: Config | None, new_config: Config) -> bool:
    """Check if router needs restart due to room changes."""
    if not config:
        return False

    old_rooms = config.get_all_configured_rooms()
    new_rooms = new_config.get_all_configured_rooms()
    return old_rooms != new_rooms


def _create_temp_user(entity_name: str, config: Config) -> AgentMatrixUser:
    """Create a temporary user object that will be updated by ensure_user_account."""
    if entity_name == ROUTER_AGENT_NAME:
        display_name = "RouterAgent"
    elif entity_name in config.agents:
        display_name = config.agents[entity_name].display_name
    elif entity_name in config.teams:
        display_name = config.teams[entity_name].display_name
    else:
        display_name = entity_name

    return AgentMatrixUser(
        agent_name=entity_name,
        user_id="",  # Will be set by ensure_user_account
        display_name=display_name,
        password="",  # Will be set by ensure_user_account
    )


async def _cancel_sync_task(entity_name: str, sync_tasks: dict[str, asyncio.Task]) -> None:
    """Cancel and remove a sync task for an entity."""
    if entity_name in sync_tasks:
        task = sync_tasks[entity_name]
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        del sync_tasks[entity_name]


async def _stop_entities(
    entities_to_restart: set[str],
    agent_bots: dict[str, Any],
    sync_tasks: dict[str, asyncio.Task],
) -> None:
    # Cancel sync tasks to prevent duplicates
    for entity_name in entities_to_restart:
        await _cancel_sync_task(entity_name, sync_tasks)

    stop_tasks = []
    for entity_name in entities_to_restart:
        if entity_name in agent_bots:
            bot = agent_bots[entity_name]
            stop_tasks.append(bot.stop())

    if stop_tasks:
        await asyncio.gather(*stop_tasks)

    for entity_name in entities_to_restart:
        agent_bots.pop(entity_name, None)


async def _sync_forever_with_restart(bot: AgentBot | TeamBot, max_retries: int = -1) -> None:
    """Run sync_forever with automatic restart on failure.

    Args:
        bot: The bot to run sync for
        max_retries: Maximum number of retries (-1 for infinite)

    """
    retry_count = 0
    while bot.running and (max_retries < 0 or retry_count < max_retries):
        try:
            logger.info(f"Starting sync loop for {bot.agent_name}")
            await bot.sync_forever()
            # If sync_forever returns normally, the bot was stopped intentionally
            break
        except asyncio.CancelledError:
            # Task was cancelled, exit gracefully
            logger.info(f"Sync task for {bot.agent_name} was cancelled")
            break
        except Exception:
            retry_count += 1
            logger.exception(f"Sync loop failed for {bot.agent_name} (retry {retry_count})")

            if not bot.running:
                # Bot was stopped, don't restart
                break

            if max_retries >= 0 and retry_count >= max_retries:
                logger.exception(f"Max retries ({max_retries}) reached for {bot.agent_name}, giving up")
                break

            # Wait a bit before restarting to avoid rapid restarts
            wait_time = min(60, 5 * retry_count)  # Exponential backoff, max 60 seconds
            logger.info(f"Restarting sync loop for {bot.agent_name} in {wait_time} seconds...")
            await asyncio.sleep(wait_time)


async def _handle_config_change(orchestrator: MultiAgentOrchestrator, stop_watching: asyncio.Event) -> None:
    """Handle configuration file changes."""
    logger.info("Configuration file changed, checking for updates...")
    if orchestrator.running:
        updated = await orchestrator.update_config()
        if updated:
            logger.info("Configuration update applied to affected agents")
        else:
            logger.info("No agent changes detected in configuration update")
    if not orchestrator.running:
        stop_watching.set()


async def _watch_config_task(config_path: Path, orchestrator: MultiAgentOrchestrator) -> None:
    """Watch config file for changes."""
    stop_watching = asyncio.Event()

    async def on_config_change() -> None:
        await _handle_config_change(orchestrator, stop_watching)

    await watch_file(config_path, on_config_change, stop_watching)


async def main(log_level: str, storage_path: Path) -> None:
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
        storage_path: The base directory for storing agent data

    """
    # Set up logging with the specified level
    setup_logging(level=log_level)

    # Sync API keys from environment to CredentialsManager
    logger.info("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()

    # Create storage directory if it doesn't exist
    storage_path.mkdir(parents=True, exist_ok=True)

    # Get config file path
    config_path = Path("config.yaml")

    # Create and start orchestrator
    logger.info("Starting orchestrator...")
    orchestrator = MultiAgentOrchestrator(storage_path=storage_path)

    try:
        # Create task to run the orchestrator
        orchestrator_task = asyncio.create_task(orchestrator.start())

        # Create task to watch config file for changes
        watcher_task = asyncio.create_task(_watch_config_task(config_path, orchestrator))

        # Wait for either orchestrator or watcher to complete
        done, pending = await asyncio.wait({orchestrator_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED)

        # Check if any completed task had an exception
        for task in done:
            try:
                task.result()  # This will raise if the task had an exception
            except asyncio.CancelledError:
                logger.info("Task was cancelled")
            except Exception:
                logger.exception("Task failed with exception")
                # Don't re-raise - let cleanup happen gracefully

        # Cancel any pending tasks
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    except Exception:
        logger.exception("Error in orchestrator")
    finally:
        # Final cleanup
        if orchestrator is not None:
            await orchestrator.stop()
