"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio

from . import interactive, voice_handler
from .agents import create_agent, get_rooms_for_entity
from .ai import ai_response, ai_response_streaming
from .background_tasks import create_background_task, wait_for_background_tasks
from .commands import (
    Command,
    CommandType,
    command_parser,
    get_command_help,
    handle_invite_command,
    handle_list_invites_command,
    handle_widget_command,
)
from .config import Config
from .constants import ROUTER_AGENT_NAME, VOICE_PREFIX
from .file_watcher import watch_file
from .logging_config import emoji, get_logger, setup_logging
from .matrix import MATRIX_HOMESERVER
from .matrix.client import (
    check_and_set_avatar,
    edit_message,
    extract_thread_info,
    fetch_thread_history,
    get_joined_rooms,
    get_room_members,
    invite_to_room,
    join_room,
    leave_room,
    send_message,
)
from .matrix.identity import (
    MatrixID,
    extract_agent_name,
    extract_server_name_from_homeserver,
)
from .matrix.mentions import create_mention_content_from_text
from .matrix.rooms import ensure_all_rooms_exist, ensure_user_in_rooms, load_rooms, resolve_room_aliases
from .matrix.state import MatrixState
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
from .streaming import IN_PROGRESS_MARKER, StreamingResponse
from .teams import TeamMode, create_team_response, get_team_model, should_form_team
from .thread_invites import ThreadInviteManager
from .thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_available_agents_in_room,
    get_safe_thread_root,
    has_user_responded_after_message,
    should_agent_respond,
)

if TYPE_CHECKING:
    import structlog
    from agno.agent import Agent

logger = get_logger(__name__)


# Constants
SYNC_TIMEOUT_MS = 30000
CLEANUP_INTERVAL_SECONDS = 3600


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
    enable_streaming = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"

    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            rooms=rooms,
            team_agents=team_config.agents,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=False,
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
    is_invited_to_thread: bool
    mentioned_agents: list[str]


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    agent_user: AgentMatrixUser
    storage_path: Path
    config: Config
    rooms: list[str] = field(default_factory=list)

    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    response_tracker: ResponseTracker = field(init=False)
    thread_invite_manager: ThreadInviteManager = field(init=False)
    invitation_timeout_hours: int = field(default=24)  # Configurable invitation timeout
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
        return MatrixID.parse(self.agent_user.user_id)

    @cached_property
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        return create_agent(agent_name=self.agent_name, storage_path=self.storage_path / "agents", config=self.config)

    async def join_configured_rooms(self) -> None:
        """Join all rooms this agent is configured for."""
        assert self.client is not None
        for room_id in self.rooms:
            if await join_room(self.client, room_id):
                self.logger.info("Joined room", room_id=room_id)
                # Only the router agent should restore scheduled tasks
                # to avoid duplicate task instances after restart
                if self.agent_name == ROUTER_AGENT_NAME:
                    restored = await restore_scheduled_tasks(self.client, room_id, self.config)
                    if restored > 0:
                        self.logger.info(f"Restored {restored} scheduled tasks in room {room_id}")
            else:
                self.logger.warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this agent is no longer configured for.

        Note: Agents will stay in rooms where they have thread invitations,
        even if not configured for the room.
        """
        assert self.client is not None
        assert self.thread_invite_manager is not None

        # Get all rooms we're currently in
        joined_rooms = await get_joined_rooms(self.client)
        if joined_rooms is None:
            return

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.rooms)

        # Leave rooms we're no longer configured for AND have no thread invitations
        for room_id in current_rooms - configured_rooms:
            agent_threads = await self.thread_invite_manager.get_agent_threads(room_id, self.agent_name)
            if agent_threads:
                self.logger.info(f"Staying in room {room_id} due to {len(agent_threads)} thread invitation(s)")
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
        # Ensure user account exists
        await self.ensure_user_account()

        # Login with the account
        self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)

        # Set avatar if available
        await self._set_avatar_if_available()

        # Initialize response tracker and thread invite manager
        self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)
        self.thread_invite_manager = ThreadInviteManager(self.client)

        # Register event callbacks
        self.client.add_event_callback(self._on_invite, nio.InviteEvent)
        self.client.add_event_callback(self._on_message, nio.RoomMessageText)
        self.client.add_event_callback(self._on_reaction, nio.ReactionEvent)

        # Register voice message callbacks (only for router agent to avoid duplicates)
        if self.agent_name == ROUTER_AGENT_NAME:
            self.client.add_event_callback(self._on_voice_message, nio.RoomMessageAudio)
            self.client.add_event_callback(self._on_voice_message, nio.RoomEncryptedAudio)

        self.running = True

        # Router bot has additional responsibilities
        if self.agent_name == ROUTER_AGENT_NAME:
            try:
                await cleanup_all_orphaned_bots(self.client, self.config, self.thread_invite_manager)
            except Exception as e:
                self.logger.warning(f"Could not cleanup orphaned bots (non-critical): {e}")

            asyncio.create_task(self._periodic_cleanup())  # noqa: RUF006

        # Note: Room joining is deferred until after invitations are handled
        self.logger.info(f"Agent setup complete: {self.agent_user.user_id}")

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

        assert self.client is not None
        await self.client.close()
        self.logger.info("Stopped agent bot")

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        assert self.client is not None
        await self.client.sync_forever(timeout=SYNC_TIMEOUT_MS, full_state=True)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        assert self.client is not None
        self.logger.info("Received invite", room_id=room.room_id, sender=event.sender)
        if await join_room(self.client, room.room_id):
            self.logger.info("Joined room", room_id=room.room_id)
        else:
            self.logger.error("Failed to join room", room_id=room.room_id)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:  # noqa: C901, PLR0911, PLR0912
        assert self.client is not None
        if event.body.rstrip().endswith(IN_PROGRESS_MARKER.strip()):
            return

        if (
            event.sender == self.agent_user.user_id
            # Allow processing of voice transcriptions the router sent on behalf of users
            and not event.body.startswith(VOICE_PREFIX)
        ):
            return

        # Check if we should process messages in this room
        # Process if: configured for room OR invited to threads in room
        if room.room_id not in self.rooms:
            assert self.thread_invite_manager is not None
            agent_threads = await self.thread_invite_manager.get_agent_threads(room.room_id, self.agent_name)
            if not agent_threads:
                # Not configured for room and no thread invitations
                return

        await interactive.handle_text_response(self.client, room, event, self.agent_name)

        sender_id = MatrixID.parse(event.sender)
        assert self.config is not None
        sender_agent_name = sender_id.agent_name(self.config)
        if sender_id.is_agent and sender_agent_name:
            assert self.thread_invite_manager is not None
            await self.thread_invite_manager.update_agent_activity(room.room_id, sender_agent_name)

        # Try to parse as command - parser handles emoji prefixes
        command = command_parser.parse(event.body)
        if command:  # ONLY router handles the command
            if self.agent_name != ROUTER_AGENT_NAME:
                return
            await self._handle_command(room, event, command)
            return

        context = await self._extract_message_context(room, event)

        is_router_self_voice = (
            self.agent_name == ROUTER_AGENT_NAME
            and event.sender == self.agent_user.user_id
            and event.body.startswith(VOICE_PREFIX)
        )

        # Ignore messages from other agents unless we are mentioned,
        # except when the router is handling its own voice transcription (VOICE_PREFIX),
        # which should be treated as a user-originated message to allow routing.
        sender_is_agent = extract_agent_name(event.sender, self.config) is not None
        if sender_is_agent and not context.am_i_mentioned and not is_router_self_voice:
            self.logger.debug("Ignoring message from other agent (not mentioned)")
            return

        # Check if message is still being streamed (has in-progress marker)
        if sender_is_agent and context.am_i_mentioned and event.body.rstrip().endswith(IN_PROGRESS_MARKER.strip()):
            self.logger.debug("Ignoring mention from agent - streaming not complete", sender=event.sender)
            return

        # Router agent has one simple job: route messages when no specific agent is mentioned
        if self.agent_name == ROUTER_AGENT_NAME:
            if not context.mentioned_agents:
                # Only route if no agents have participated in the thread yet
                agents_in_thread = get_agents_in_thread(context.thread_history, self.config)
                if not agents_in_thread:
                    await self._handle_ai_routing(room, event, context.thread_history)
            return

        if self._should_skip_duplicate_response(event):
            return

        # Check if we should form a team first
        agents_in_thread = get_agents_in_thread(context.thread_history, self.config)  # Excludes router
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(context.thread_history, self.config)
        form_team = await should_form_team(
            context.mentioned_agents,
            agents_in_thread,
            all_mentioned_in_thread,
            message=event.body,
            config=self.config,
        )

        # Simple team formation: only the first agent (alphabetically) handles team formation
        if form_team.should_form_team and self.agent_name in form_team.agents:
            # Simple coordination: let the first agent alphabetically handle the team
            first_agent = min(form_team.agents)
            if self.agent_name != first_agent:
                # Other agents in the team don't respond individually
                return

            # Create and execute team response
            model_name = get_team_model(self.agent_name, room.room_id, self.config)
            team_response = await create_team_response(
                agent_names=form_team.agents,
                mode=form_team.mode,
                message=event.body,
                orchestrator=self.orchestrator,
                thread_history=context.thread_history,
                model_name=model_name,
            )
            await self._send_response(room, event.event_id, team_response, context.thread_id)
            # Mark as responded after team response
            self.response_tracker.mark_responded(event.event_id)
            return

        # Determine if this agent should respond individually
        should_respond = should_agent_respond(
            agent_name=self.agent_name,
            am_i_mentioned=context.am_i_mentioned,
            is_thread=context.is_thread,
            room_id=room.room_id,
            configured_rooms=self.rooms,
            thread_history=context.thread_history,
            config=self.config,
            is_invited_to_thread=context.is_invited_to_thread,
            mentioned_agents=context.mentioned_agents,
        )

        if should_respond and not context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        if not should_respond:
            return

        # Process and send response
        self.logger.info("Processing", event_id=event.event_id)
        await self._generate_response(
            room_id=room.room_id,
            prompt=event.body,
            reply_to_event_id=event.event_id,
            thread_id=context.thread_id,
            thread_history=context.thread_history,
            user_id=event.sender,
        )
        # Mark as responded after response generation
        self.response_tracker.mark_responded(event.event_id)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions."""
        assert self.client is not None
        result = await interactive.handle_reaction(self.client, event, self.agent_name, self.config)

        if result:
            selected_value, thread_id = result
            # User selected an option from an interactive question

            # Check if we should process this reaction
            thread_history = []
            if thread_id:
                thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)
                if has_user_responded_after_message(thread_history, event.reacts_to, self.client.user_id):
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
                room,
                None if thread_id else event.reacts_to,
                ack_text,
                thread_id,
            )

            if not ack_event_id:
                self.logger.error("Failed to send acknowledgment for reaction")
                return

            # Thread history already fetched above, no need to fetch again

            # Generate the response, editing the acknowledgment message
            prompt = f"The user selected: {selected_value}"
            await self._generate_response(
                room_id=room.room_id,
                prompt=prompt,
                reply_to_event_id=event.reacts_to,
                thread_id=thread_id,
                thread_history=thread_history,
                existing_event_id=ack_event_id,  # Edit the acknowledgment
                user_id=event.sender,
            )
            # Mark the original interactive question as responded
            self.response_tracker.mark_responded(event.reacts_to)

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
        if event.sender == self.agent_user.user_id:
            return

        # Check if we've already responded to this voice message (e.g., after restart)
        if self.response_tracker.has_responded(event.event_id):
            self.logger.debug("Already processed voice message", event_id=event.event_id)
            return

        self.logger.info("Processing voice message", event_id=event.event_id, sender=event.sender)

        transcribed_message = await voice_handler.handle_voice_message(self.client, room, event, self.config)

        if transcribed_message:
            is_thread, thread_id = extract_thread_info(event.source)

            await self._send_response(
                room=room,
                reply_to_event_id=event.event_id,
                response_text=transcribed_message,
                thread_id=thread_id,
            )

        # Mark the voice message as responded so we don't process it again
        self.response_tracker.mark_responded(event.event_id)

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> MessageContext:
        assert self.client is not None
        assert self.thread_invite_manager is not None

        # Check if mentions should be ignored for this message
        skip_mentions = _should_skip_mentions(event.source)

        if skip_mentions:
            # Don't detect mentions if the message has skip_mentions metadata
            mentioned_agents: list[str] = []
            am_i_mentioned = False
        else:
            mentioned_agents, am_i_mentioned = check_agent_mentioned(event.source, self.agent_name, self.config)

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id, room_name=room.name)

        is_thread, thread_id = extract_thread_info(event.source)

        thread_history = []
        is_invited_to_thread = False
        if thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)
            is_invited_to_thread = await self.thread_invite_manager.is_agent_invited_to_thread(
                thread_id,
                room.room_id,
                self.agent_name,
            )

        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            is_invited_to_thread=is_invited_to_thread,
            mentioned_agents=mentioned_agents,
        )

    async def _process_and_respond(
        self,
        room: nio.MatrixRoom,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
    ) -> None:
        """Process a message and send a response (non-streaming)."""
        if not prompt.strip():
            return

        session_id = create_session_id(room.room_id, thread_id)

        response_text = await ai_response(
            agent_name=self.agent_name,
            prompt=prompt,
            session_id=session_id,
            storage_path=self.storage_path,
            config=self.config,
            thread_history=thread_history,
            room_id=room.room_id,
        )

        if existing_event_id:
            # Edit the existing message
            await self._edit_message(room.room_id, existing_event_id, response_text, thread_id)
            return

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self._send_response(room, reply_to_event_id, response.formatted_text, thread_id)
        if event_id and response.option_map and response.options_list:
            interactive.register_interactive_question(
                event_id,
                room.room_id,
                thread_id,
                response.option_map,
                self.agent_name,
            )
            await interactive.add_reaction_buttons(self.client, room.room_id, event_id, response.options_list)

    async def _process_and_respond_streaming(
        self,
        room: nio.MatrixRoom,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
    ) -> None:
        """Process a message and send a response (streaming)."""
        assert self.client is not None
        if not prompt.strip():
            return

        session_id = create_session_id(room.room_id, thread_id)
        sender_id = self.matrix_id

        streaming = StreamingResponse(
            room_id=room.room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            sender_domain=sender_id.domain,
            config=self.config,
        )

        # If we're editing an existing message, set the event_id
        if existing_event_id:
            streaming.event_id = existing_event_id
            streaming.accumulated_text = ""  # Start fresh

        try:
            async for chunk in ai_response_streaming(
                agent_name=self.agent_name,
                prompt=prompt,
                session_id=session_id,
                storage_path=self.storage_path,
                config=self.config,
                thread_history=thread_history,
                room_id=room.room_id,
            ):
                await streaming.update_content(chunk, self.client)

            await streaming.finalize(self.client)

            if streaming.event_id:
                self.logger.info("Sent streaming response", event_id=streaming.event_id)

        except Exception as e:
            self.logger.exception("Error in streaming response", error=str(e))
            # Don't mark as responded if streaming failed

        # If the message contains an interactive question, register it and add reactions
        if streaming.event_id and interactive.should_create_interactive_question(streaming.accumulated_text):
            response = interactive.parse_and_format_interactive(streaming.accumulated_text, extract_mapping=True)
            if response.option_map and response.options_list:
                interactive.register_interactive_question(
                    streaming.event_id,
                    room.room_id,
                    thread_id,
                    response.option_map,
                    self.agent_name,
                )
                await interactive.add_reaction_buttons(
                    self.client,
                    room.room_id,
                    streaming.event_id,
                    response.options_list,
                )

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
        """Generate and send/edit a response using AI.

        Args:
            room_id: The room to send the response to
            prompt: The prompt to send to the AI
            reply_to_event_id: The event to reply to
            thread_id: Thread ID if in a thread
            thread_history: Thread history for context
            existing_event_id: If provided, edit this message instead of sending a new one
            user_id: User ID of the sender for identifying user messages in history

        """
        if not prompt.strip():
            return

        assert self.client is not None
        room = nio.MatrixRoom(room_id=room_id, own_user_id=self.client.user_id)

        # Store memory for this agent (do this once, before generating response)
        session_id = create_session_id(room_id, thread_id)
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

        # Dispatch to appropriate method
        if self.enable_streaming:
            await self._process_and_respond_streaming(
                room,
                prompt,
                reply_to_event_id,
                thread_id,
                thread_history,
                existing_event_id,
            )
        else:
            await self._process_and_respond(
                room,
                prompt,
                reply_to_event_id,
                thread_id,
                thread_history,
                existing_event_id,
            )

    async def _send_response(
        self,
        room: nio.MatrixRoom,
        reply_to_event_id: str | None,
        response_text: str,
        thread_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
        skip_mentions: bool = False,
    ) -> str | None:
        """Send a response message to a room.

        Args:
            room: The room to send to
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
        effective_thread_id = thread_id or get_safe_thread_root(reply_to_event) or reply_to_event_id

        content = create_mention_content_from_text(
            self.config,
            response_text,
            sender_domain=sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=reply_to_event_id,
        )

        # Add metadata to indicate mentions should be ignored for responses
        if skip_mentions:
            content["com.mindroom.skip_mentions"] = True

        assert self.client is not None
        event_id = await send_message(self.client, room.room_id, content)
        if event_id:
            self.logger.info("Sent response", event_id=event_id, room_name=room.name)
            return event_id
        self.logger.error("Failed to send response to room", room_id=room.room_id)
        return None

    async def _edit_message(self, room_id: str, event_id: str, new_text: str, thread_id: str | None) -> bool:
        """Edit an existing message.

        Returns:
            True if edit was successful, False otherwise.

        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        content = create_mention_content_from_text(
            self.config,
            new_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
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

        available_agents = get_available_agents_in_room(room, self.config)
        if not available_agents:
            self.logger.debug("No available agents to route to")
            return

        self.logger.info("Handling AI routing", event_id=event.event_id)

        _, thread_event_id = extract_thread_info(event.source)
        suggested_agent = await suggest_agent_for_message(
            event.body,
            available_agents,
            self.config,
            thread_history,
            thread_event_id,
            room.room_id,
            self.thread_invite_manager,
        )
        if not suggested_agent:
            return

        # Router mentions the suggested agent and asks them to help
        response_text = f"@{suggested_agent} could you help with this?"
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        # If no thread exists, create one with the original message as root
        if not thread_event_id:
            thread_event_id = event.event_id

        content = create_mention_content_from_text(
            self.config,
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_event_id,
            reply_to_event_id=event.event_id,
        )

        assert self.client is not None
        event_id = await send_message(self.client, room.room_id, content)
        if event_id:
            self.logger.info("Routed to agent", suggested_agent=suggested_agent)
            self.response_tracker.mark_responded(event.event_id)
        else:
            self.logger.error("Failed to route to agent", agent=suggested_agent)

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:  # noqa: C901, PLR0912
        self.logger.info("Handling command", command_type=command.type.value)

        is_thread, thread_id = extract_thread_info(event.source)

        # Widget command modifies room state, so it doesn't need a thread
        if command.type == CommandType.WIDGET:
            assert self.client is not None
            url = command.args.get("url")
            response_text = await handle_widget_command(client=self.client, room_id=room.room_id, url=url)
            # Send response in thread if in thread, otherwise in main room
            await self._send_response(room, event.event_id, response_text, thread_id)
            return

        # For commands that need thread context, use the existing thread or the event will start a new one
        # The _send_response method will automatically create a thread if needed
        effective_thread_id = thread_id or event.event_id

        response_text = ""

        if command.type == CommandType.INVITE:
            # Handle invite command
            agent_name = command.args["agent_name"]
            agent_domain = self.matrix_id.domain

            assert self.client is not None
            assert self.thread_invite_manager is not None
            response_text = await handle_invite_command(
                room_id=room.room_id,
                thread_id=effective_thread_id,
                agent_name=agent_name,
                sender=event.sender,
                agent_domain=agent_domain,
                client=self.client,
                thread_invite_manager=self.thread_invite_manager,
                config=self.config,
            )

        elif command.type == CommandType.UNINVITE:
            agent_name = command.args["agent_name"]
            assert self.thread_invite_manager is not None
            removed = await self.thread_invite_manager.remove_invite(effective_thread_id, room.room_id, agent_name)
            if removed:
                response_text = f"✅ Removed @{agent_name} from this thread."
            else:
                response_text = f"❌ @{agent_name} was not invited to this thread."

        elif command.type == CommandType.LIST_INVITES:
            assert self.thread_invite_manager is not None
            response_text = await handle_list_invites_command(
                room.room_id,
                effective_thread_id,
                self.thread_invite_manager,
            )

        elif command.type == CommandType.HELP:
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        elif command.type == CommandType.SCHEDULE:
            full_text = command.args["full_text"]

            assert self.client is not None
            task_id, response_text = await schedule_task(
                client=self.client,
                room_id=room.room_id,
                thread_id=effective_thread_id,
                agent_user_id=self.agent_user.user_id,
                scheduled_by=event.sender,
                full_text=full_text,
                config=self.config,
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

        elif command.type == CommandType.UNKNOWN:
            # Handle unknown commands
            response_text = "❌ Unknown command. Try !help for available commands."

        if response_text:
            await self._send_response(
                room,
                event.event_id,
                response_text,
                thread_id,
                reply_to_event=event,
                skip_mentions=True,
            )
            self.response_tracker.mark_responded(event.event_id)

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired thread invitations."""
        while self.running:
            try:
                # Wait for 1 hour between cleanups
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

                # Get all rooms the bot is in
                assert self.client is not None
                joined_rooms = await get_joined_rooms(self.client)
                if joined_rooms is None:
                    continue

                total_removed = 0
                for room_id in joined_rooms:
                    try:
                        assert self.thread_invite_manager is not None
                        removed_count = await self.thread_invite_manager.cleanup_inactive_agents(
                            room_id,
                            timeout_hours=self.invitation_timeout_hours,
                        )
                        total_removed += removed_count
                    except Exception as e:
                        self.logger.exception("Failed to cleanup room", room_id=room_id, error=str(e))

                if total_removed > 0:
                    self.logger.info(f"Periodic cleanup removed {total_removed} expired agents")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.exception("Error in periodic cleanup", error=str(e))

    def _should_skip_duplicate_response(self, event: nio.RoomMessageText) -> bool:
        """Check if we should skip responding to avoid duplicates.

        This handles two cases:
        1. We've already responded to this exact event
        2. This is an edit of a message we've already responded to (from users)

        Note: Edits from agents are filtered earlier in _on_message to avoid
        responding to incomplete streaming messages.

        Args:
            event: The Matrix message event

        Returns:
            True if we should skip processing this message

        """
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        is_edit = relates_to.get("rel_type") == "m.replace"

        if is_edit:
            original_event_id = relates_to.get("event_id")
            if original_event_id and self.response_tracker.has_responded(original_event_id):
                self.logger.debug("Ignoring edit of already-responded message", original_event_id=original_event_id)
                return True
        elif self.response_tracker.has_responded(event.event_id):
            return True

        return False


@dataclass
class TeamBot(AgentBot):
    """A bot that represents a team of agents working together."""

    team_agents: list[str] = field(default_factory=list)
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

        # Get the appropriate model for this team and room
        model_name = get_team_model(self.agent_name, room_id, self.config)

        # Convert team_mode string to TeamMode enum
        mode = TeamMode.COORDINATE if self.team_mode == "coordinate" else TeamMode.COLLABORATE

        # Create team response
        response_text = await create_team_response(
            agent_names=self.team_agents,
            mode=mode,
            message=prompt,
            orchestrator=self.orchestrator,
            thread_history=thread_history,
            model_name=model_name,
        )

        # Store memory once for the entire team (avoids duplicate LLM processing)
        session_id = create_session_id(room_id, thread_id)
        create_background_task(
            store_conversation_memory(
                prompt,
                self.team_agents,  # Pass list of agents for team storage
                self.storage_path,
                session_id,
                self.config,
                room_id,
                thread_history,
                user_id,
            ),
            name=f"memory_save_team_{session_id}",
        )
        self.logger.info(f"Storing memory for team: {self.team_agents}")

        # Send the response (reuse parent's method for consistency)
        assert self.client is not None
        room = nio.MatrixRoom(room_id=room_id, own_user_id=self.client.user_id)

        if existing_event_id:
            await self._edit_message(room_id, existing_event_id, response_text, thread_id)
        else:
            # Send as regular message (not streaming for teams)
            await self._send_response(room, reply_to_event_id, response_text, thread_id)


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot | TeamBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)
    config: Config | None = field(default=None, init=False)
    _created_room_ids: dict[str, str] = field(default_factory=dict, init=False)

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
        start_tasks = [bot.start() for bot in self.agent_bots.values()]
        await asyncio.gather(*start_tasks)
        self.running = True
        logger.info("All agent bots started successfully")

        # Setup rooms and have all bots join them
        await self._setup_rooms_and_memberships(list(self.agent_bots.values()))

        # Create sync tasks for each bot
        sync_tasks = []
        for bot in self.agent_bots.values():
            # Create a task for each bot's sync loop
            sync_task = asyncio.create_task(bot.sync_forever())
            sync_tasks.append(sync_task)

        # Run all sync tasks
        await asyncio.gather(*sync_tasks)

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

        if not entities_to_restart and not new_entities:
            self.config = new_config
            return False

        # Stop entities that need restarting
        if entities_to_restart:
            await _stop_entities(entities_to_restart, self.agent_bots)

        # Update config
        self.config = new_config

        # Update config for all existing bots that aren't being restarted
        for entity_name, bot in self.agent_bots.items():
            if entity_name not in entities_to_restart:
                bot.config = new_config

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
                    await bot.start()
                    # Start sync loop
                    asyncio.create_task(bot.sync_forever())  # noqa: RUF006
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
                await bot.start()
                asyncio.create_task(bot.sync_forever())  # noqa: RUF006

        # Handle removed entities (cleanup)
        removed_entities = existing_entities - all_new_entities
        for entity_name in removed_entities:
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

        # Store room IDs for later use
        self._created_room_ids = room_ids

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
        if old_agent != new_agent and (agent_name in agent_bots or new_agent is not None):
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
        if old_team != new_team and (team_name in agent_bots or new_team is not None):
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


async def _stop_entities(entities_to_restart: set[str], agent_bots: dict[str, Any]) -> None:
    stop_tasks = []
    for entity_name in entities_to_restart:
        if entity_name in agent_bots:
            bot = agent_bots[entity_name]
            stop_tasks.append(bot.stop())

    if stop_tasks:
        await asyncio.gather(*stop_tasks)

    for entity_name in entities_to_restart:
        agent_bots.pop(entity_name, None)


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
