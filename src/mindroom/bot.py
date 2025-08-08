"""Multi-agent bot implementation where each agent has its own Matrix user account."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import nio

from . import interactive
from .agent_config import ROUTER_AGENT_NAME, create_agent, load_config
from .ai import ai_response, ai_response_streaming
from .background_tasks import wait_for_background_tasks
from .commands import (
    Command,
    CommandType,
    command_parser,
    get_command_help,
    handle_invite_command,
    handle_list_invites_command,
    handle_widget_command,
)
from .logging_config import emoji, get_logger, setup_logging
from .matrix import (
    MATRIX_HOMESERVER,
    AgentMatrixUser,
    MatrixID,
    create_mention_content_from_text,
    edit_message,
    ensure_all_agent_users,
    extract_agent_name,
    extract_thread_info,
    fetch_thread_history,
    join_room,
    login_agent_user,
    resolve_room_aliases,
)
from .models import Config
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .scheduling import (
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
    from agno.agent import Agent

logger = get_logger(__name__)

# Constants
SYNC_TIMEOUT_MS = 30000
CLEANUP_INTERVAL_SECONDS = 3600


def get_all_configured_rooms(config: Config) -> set[str]:
    """Extract all room aliases configured for agents and teams."""
    all_room_aliases = set()
    for agent_config in config.agents.values():
        all_room_aliases.update(agent_config.rooms)
    for team_config in config.teams.values():
        all_room_aliases.update(team_config.rooms)
    return all_room_aliases


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
        all_room_aliases = get_all_configured_rooms(config)
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, rooms, enable_streaming=enable_streaming)

    elif entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            rooms=rooms,
            team_agents=team_config.agents,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=False,
        )

    elif entity_name in config.agents:
        agent_config = config.agents[entity_name]
        rooms = resolve_room_aliases(agent_config.rooms)
        return AgentBot(agent_user, storage_path, rooms, enable_streaming=enable_streaming)

    return None


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
    rooms: list[str] = field(default_factory=list)

    client: nio.AsyncClient = field(init=False)
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
    def logger(self):
        """Get a logger with agent context bound."""
        return logger.bind(agent=emoji(self.agent_name))

    @cached_property
    def matrix_id(self) -> MatrixID:
        """Get the Matrix ID for this agent bot."""
        return MatrixID.parse(self.agent_user.user_id)

    @cached_property
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        return create_agent(agent_name=self.agent_name, storage_path=self.storage_path / "agents")

    async def start(self) -> None:
        """Start the agent bot."""
        self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)

        # Initialize response tracker
        self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)

        # Initialize thread invite manager
        self.thread_invite_manager = ThreadInviteManager(self.client)

        self.client.add_event_callback(self._on_invite, nio.InviteEvent)
        self.client.add_event_callback(self._on_message, nio.RoomMessageText)
        self.client.add_event_callback(self._on_reaction, nio.ReactionEvent)

        self.running = True
        self.logger.info("Started bot", user_id=self.agent_user.user_id)

        # Router bot cleans up orphaned bots from all rooms on startup
        if self.agent_name == ROUTER_AGENT_NAME:
            from .room_cleanup import cleanup_all_orphaned_bots

            self.logger.info("Router bot checking for orphaned bots in all rooms...")
            try:
                kicked = await cleanup_all_orphaned_bots(self.client)
                if kicked:
                    self.logger.info(f"Cleaned up orphaned bots from {len(kicked)} rooms")
            except Exception as e:
                self.logger.error(f"Failed to cleanup orphaned bots: {e}")

        # Join configured rooms
        for room_id in self.rooms:
            if await join_room(self.client, room_id):
                self.logger.info("Joined room", room_id=room_id)
                # Restore scheduled tasks for this room
                restored = await restore_scheduled_tasks(self.client, room_id)
                if restored > 0:
                    self.logger.info(f"Restored {restored} scheduled tasks in room {room_id}")
            else:
                self.logger.warning("Failed to join room", room_id=room_id)

        # Start periodic cleanup task for the general agent only
        if self.agent_name == "general":
            asyncio.create_task(self._periodic_cleanup())

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False

        # Wait for any pending background tasks (like memory saves) to complete
        try:
            await wait_for_background_tasks(timeout=5.0)  # 5 second timeout
            self.logger.info("Background tasks completed")
        except Exception as e:
            self.logger.warning(f"Some background tasks did not complete: {e}")

        if hasattr(self, "client") and self.client:
            await self.client.close()
        self.logger.info("Stopped agent bot")

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        if not hasattr(self, "client") or not self.client:
            self.logger.error("Cannot sync - client not initialized")
            return
        await self.client.sync_forever(timeout=SYNC_TIMEOUT_MS, full_state=True)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        self.logger.info("Received invite", room_id=room.room_id, sender=event.sender)
        if await join_room(self.client, room.room_id):
            self.logger.info("Joined room", room_id=room.room_id)
        else:
            self.logger.error("Failed to join room", room_id=room.room_id)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        if event.sender == self.agent_user.user_id:
            return

        if room.room_id not in self.rooms:
            return

        await interactive.handle_text_response(self.client, room, event, self.agent_name)

        sender_id = MatrixID.parse(event.sender)

        if sender_id.is_agent and sender_id.agent_name:
            await self.thread_invite_manager.update_agent_activity(room.room_id, sender_id.agent_name)

        is_command = event.body.strip().startswith("!")
        if is_command:  # ONLY router handles the command
            if self.agent_name != ROUTER_AGENT_NAME:
                return
            command = command_parser.parse(event.body)
            if command:
                await self._handle_command(room, event, command)
            else:
                help_text = "❌ Unknown command. Try !help for available commands."
                await self._send_response(room, event.event_id, help_text, thread_id=None, reply_to_event=event)
            return

        context = await self._extract_message_context(room, event)

        # If message is from another agent and we're not mentioned, ignore it
        sender_is_agent = extract_agent_name(event.sender) is not None
        if sender_is_agent and not context.am_i_mentioned:
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
                agents_in_thread = get_agents_in_thread(context.thread_history)
                if not agents_in_thread:
                    await self._handle_ai_routing(room, event, context.thread_history)
            return

        if self._should_skip_duplicate_response(event):
            return

        # Check if we should form a team first
        agents_in_thread = get_agents_in_thread(context.thread_history)
        all_mentioned_in_thread = get_all_mentioned_agents_in_thread(context.thread_history)
        form_team = should_form_team(context.mentioned_agents, agents_in_thread, all_mentioned_in_thread)

        # Simple team formation: only the first agent (alphabetically) handles team formation
        if form_team.should_form_team and self.agent_name in form_team.agents:
            # Simple coordination: let the first agent alphabetically handle the team
            first_agent = min(form_team.agents)
            if self.agent_name != first_agent:
                # Other agents in the team don't respond individually
                return

            # Create and execute team response
            team_response = await create_team_response(
                agent_names=form_team.agents,
                mode=form_team.mode,
                message=event.body,
                orchestrator=self.orchestrator,
                thread_history=context.thread_history,
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
        )
        # Mark as responded after response generation
        self.response_tracker.mark_responded(event.event_id)

    async def _on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle reaction events for interactive questions."""
        result = await interactive.handle_reaction(self.client, room, event, self.agent_name)

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
            )
            # Mark the original interactive question as responded
            self.response_tracker.mark_responded(event.reacts_to)

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> MessageContext:
        mentioned_agents, am_i_mentioned = check_agent_mentioned(event.source, self.agent_name)

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id, room_name=room.name)

        is_thread, thread_id = extract_thread_info(event.source)

        thread_history = []
        is_invited_to_thread = False
        if thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)
            is_invited_to_thread = await self.thread_invite_manager.is_agent_invited_to_thread(
                thread_id, room.room_id, self.agent_name
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
                event_id, room.room_id, thread_id, response.option_map, self.agent_name
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
        if not prompt.strip():
            return

        session_id = create_session_id(room.room_id, thread_id)
        sender_id = self.matrix_id

        streaming = StreamingResponse(
            room_id=room.room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            sender_domain=sender_id.domain,
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
                    streaming.event_id, room.room_id, thread_id, response.option_map, self.agent_name
                )
                await interactive.add_reaction_buttons(
                    self.client, room.room_id, streaming.event_id, response.options_list
                )

    async def _generate_response(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
    ) -> None:
        """Generate and send/edit a response using AI.

        Args:
            room_id: The room to send the response to
            prompt: The prompt to send to the AI
            reply_to_event_id: The event to reply to
            thread_id: Thread ID if in a thread
            thread_history: Thread history for context
            existing_event_id: If provided, edit this message instead of sending a new one
        """
        if not prompt.strip():
            return

        room = nio.MatrixRoom(room_id=room_id, own_user_id=self.client.user_id)

        # Dispatch to appropriate method
        if self.enable_streaming:
            await self._process_and_respond_streaming(
                room, prompt, reply_to_event_id, thread_id, thread_history, existing_event_id
            )
        else:
            await self._process_and_respond(
                room, prompt, reply_to_event_id, thread_id, thread_history, existing_event_id
            )

    async def _send_response(
        self,
        room: nio.MatrixRoom,
        reply_to_event_id: str | None,
        response_text: str,
        thread_id: str | None,
        reply_to_event: nio.RoomMessageText | None = None,
    ) -> str | None:
        """Send a response message to a room.

        Args:
            room: The room to send to
            reply_to_event_id: The event ID to reply to (can be None when in a thread)
            response_text: The text to send
            thread_id: The thread ID if already in a thread
            reply_to_event: Optional event object for the message we're replying to (used to check for safe thread root)

        Returns:
            Event ID if message was sent successfully, None otherwise.
        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        # Always ensure we have a thread_id - use the original message as thread root if needed
        # This ensures agents always respond in threads, even when mentioned in main room
        effective_thread_id = thread_id or get_safe_thread_root(reply_to_event) or reply_to_event_id

        content = create_mention_content_from_text(
            response_text,
            sender_domain=sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=reply_to_event_id,
        )

        response = await self.client.room_send(room_id=room.room_id, message_type="m.room.message", content=content)
        if isinstance(response, nio.RoomSendResponse):
            self.logger.info("Sent response", event_id=response.event_id, room_name=room.name)
            return response.event_id  # type: ignore[no-any-return]
        else:
            self.logger.error("Failed to send response", error=str(response))
            return None

    async def _edit_message(self, room_id: str, event_id: str, new_text: str, thread_id: str | None) -> bool:
        """Edit an existing message.

        Returns:
            True if edit was successful, False otherwise.
        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        content = create_mention_content_from_text(
            new_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
        )

        response = await edit_message(self.client, room_id, event_id, content, new_text)

        if isinstance(response, nio.RoomSendResponse):
            self.logger.info("Edited message", event_id=event_id)
            return True
        else:
            self.logger.error("Failed to edit message", event_id=event_id, error=str(response))
            return False

    async def _handle_ai_routing(
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_history: list[dict]
    ) -> None:
        # Only router agent should handle routing
        if self.agent_name != ROUTER_AGENT_NAME:
            return

        available_agents = get_available_agents_in_room(room)
        if not available_agents:
            self.logger.debug("No available agents to route to")
            return

        self.logger.info("Handling AI routing", event_id=event.event_id)

        _, thread_event_id = extract_thread_info(event.source)
        suggested_agent = await suggest_agent_for_message(
            event.body,
            available_agents,
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
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_event_id,
            reply_to_event_id=event.event_id,
        )

        response = await self.client.room_send(room_id=room.room_id, message_type="m.room.message", content=content)
        if isinstance(response, nio.RoomSendResponse):
            self.logger.info("Routed to agent", suggested_agent=suggested_agent)
        else:
            self.logger.error("Failed to route to agent", agent=suggested_agent, error=str(response))

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:
        self.logger.info("Handling command", command_type=command.type.value)

        is_thread, thread_id = extract_thread_info(event.source)

        # Widget command modifies room state, so it doesn't need a thread
        if command.type == CommandType.WIDGET:
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

            response_text = await handle_invite_command(
                room_id=room.room_id,
                thread_id=effective_thread_id,
                agent_name=agent_name,
                sender=event.sender,
                agent_domain=agent_domain,
                client=self.client,
                thread_invite_manager=self.thread_invite_manager,
            )

        elif command.type == CommandType.UNINVITE:
            agent_name = command.args["agent_name"]
            removed = await self.thread_invite_manager.remove_invite(effective_thread_id, room.room_id, agent_name)
            if removed:
                response_text = f"✅ Removed @{agent_name} from this thread."
            else:
                response_text = f"❌ @{agent_name} was not invited to this thread."

        elif command.type == CommandType.LIST_INVITES:
            response_text = await handle_list_invites_command(
                room.room_id, effective_thread_id, self.thread_invite_manager
            )

        elif command.type == CommandType.HELP:
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        elif command.type == CommandType.SCHEDULE:
            full_text = command.args["full_text"]

            task_id, response_text = await schedule_task(
                client=self.client,
                room_id=room.room_id,
                thread_id=effective_thread_id,
                agent_user_id=self.agent_user.user_id,
                scheduled_by=event.sender,
                full_text=full_text,
            )

        elif command.type == CommandType.LIST_SCHEDULES:
            response_text = await list_scheduled_tasks(
                client=self.client,
                room_id=room.room_id,
                thread_id=effective_thread_id,
            )

        elif command.type == CommandType.CANCEL_SCHEDULE:
            task_id = command.args["task_id"]
            response_text = await cancel_scheduled_task(
                client=self.client,
                room_id=room.room_id,
                task_id=task_id,
            )

        if response_text:
            await self._send_response(room, event.event_id, response_text, thread_id, reply_to_event=event)

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired thread invitations."""
        while self.running:
            try:
                # Wait for 1 hour between cleanups
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

                # Get all rooms the bot is in
                joined_rooms_response = await self.client.joined_rooms()
                if not isinstance(joined_rooms_response, nio.JoinedRoomsResponse):
                    self.logger.error("Failed to get joined rooms for cleanup")
                    continue

                total_removed = 0
                for room_id in joined_rooms_response.rooms:
                    try:
                        removed_count = await self.thread_invite_manager.cleanup_inactive_agents(
                            room_id, timeout_hours=self.invitation_timeout_hours
                        )
                        total_removed += removed_count
                    except Exception as e:
                        self.logger.error("Failed to cleanup room", room_id=room_id, error=str(e))

                if total_removed > 0:
                    self.logger.info(f"Periodic cleanup removed {total_removed} expired agents")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in periodic cleanup", error=str(e))

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
        else:
            if self.response_tracker.has_responded(event.event_id):
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
    ) -> None:
        """Generate a team response instead of individual agent response."""
        if not prompt.strip():
            return

        # Get the appropriate model for this team and room
        model_name = get_team_model(self.agent_name, room_id)

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

        # Send the response (reuse parent's method for consistency)
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
    current_config: Config | None = field(default=None, init=False)

    async def initialize(self) -> None:
        """Initialize all agent bots."""
        logger.info("Initializing multi-agent system...")

        config = load_config()
        self.current_config = config
        agent_users = await ensure_all_agent_users(MATRIX_HOMESERVER)

        for agent_name, agent_user in agent_users.items():
            bot = create_bot_for_entity(agent_name, agent_user, config, self.storage_path)
            if bot is None:
                raise ValueError(f"Unknown agent configuration for {agent_name}")

            bot.orchestrator = self
            self.agent_bots[agent_name] = bot

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots."""
        if not self.agent_bots:
            await self.initialize()

        # Start each agent bot (this registers callbacks and logs in)
        start_tasks = [bot.start() for bot in self.agent_bots.values()]
        await asyncio.gather(*start_tasks)
        self.running = True
        logger.info("All agent bots started successfully")

        # Create sync tasks for each bot
        sync_tasks = []
        for bot in self.agent_bots.values():
            # Create a task for each bot's sync loop
            sync_task = asyncio.create_task(bot.sync_forever())
            sync_tasks.append(sync_task)

        # Run all sync tasks
        await asyncio.gather(*sync_tasks)

    async def update_config(self) -> bool:
        """Update configuration and restart only changed agents.

        Returns:
            True if any agents were updated, False otherwise.
        """
        # load_config now automatically uses the latest file based on modification time
        new_config = load_config()

        if not self.current_config:
            self.current_config = new_config
            return False

        agent_users = await ensure_all_agent_users(MATRIX_HOMESERVER)

        agents_to_restart = set()
        teams_to_restart = set()

        # Check for agent changes - compare each agent individually
        for agent_name in set(self.current_config.agents.keys()) | set(new_config.agents.keys()):
            old_agent = self.current_config.agents.get(agent_name)
            new_agent = new_config.agents.get(agent_name)

            if old_agent != new_agent and (agent_name in self.agent_bots or new_agent is not None):
                # Only restart if the agent bot exists OR if it's a new agent
                agents_to_restart.add(agent_name)
                if old_agent and new_agent:
                    # Log specific changes for debugging
                    if old_agent.rooms != new_agent.rooms:
                        logger.info(
                            f"Room assignments changed for agent {agent_name}: {old_agent.rooms} -> {new_agent.rooms}"
                        )
                    else:
                        logger.info(f"Configuration changed for agent {agent_name}")
                elif not old_agent:
                    logger.info(f"New agent {agent_name} added")
                else:
                    logger.info(f"Agent {agent_name} removed")

        # Check for team changes - compare each team individually
        for team_name in set(self.current_config.teams.keys()) | set(new_config.teams.keys()):
            old_team = self.current_config.teams.get(team_name)
            new_team = new_config.teams.get(team_name)

            if old_team != new_team and (team_name in self.agent_bots or new_team is not None):
                # Only restart if the team bot exists OR if it's a new team
                teams_to_restart.add(team_name)
                if old_team and new_team:
                    # Log specific changes for debugging
                    if old_team.rooms != new_team.rooms:
                        logger.info(
                            f"Room assignments changed for team {team_name}: {old_team.rooms} -> {new_team.rooms}"
                        )
                    else:
                        logger.info(f"Configuration changed for team {team_name}")
                elif not old_team:
                    logger.info(f"New team {team_name} added")
                else:
                    logger.info(f"Team {team_name} removed")

        # Check if router needs restart
        # Router only needs to restart if room assignments changed (needs to join/leave rooms)
        # Note: Router loads its model dynamically on each routing decision, so model changes
        # don't require a restart
        router_needs_restart = False
        if ROUTER_AGENT_NAME in agent_users:
            old_rooms = get_all_configured_rooms(self.current_config)
            new_rooms = get_all_configured_rooms(new_config)

            # Check room changes
            if old_rooms != new_rooms:
                router_needs_restart = True
                logger.info("Router room assignments changed")

        # Collect all entities to restart
        entities_to_restart = agents_to_restart | teams_to_restart
        if router_needs_restart:
            entities_to_restart.add(ROUTER_AGENT_NAME)

        if not entities_to_restart:
            logger.info("No configuration changes detected")
            self.current_config = new_config
            return False

        # Stop affected bots
        stop_tasks = []
        for entity_name in entities_to_restart:
            if entity_name in self.agent_bots:
                bot = self.agent_bots[entity_name]
                stop_tasks.append(bot.stop())

        if stop_tasks:
            logger.info(f"Stopping {len(stop_tasks)} bots...")
            await asyncio.gather(*stop_tasks)

        # Remove stopped bots
        for entity_name in entities_to_restart:
            self.agent_bots.pop(entity_name, None)

        # Update stored config
        self.current_config = new_config

        # Recreate bots
        for entity_name in entities_to_restart:
            if entity_name not in agent_users:
                continue

            agent_user = agent_users[entity_name]
            bot = create_bot_for_entity(entity_name, agent_user, new_config, self.storage_path)  # type: ignore[assignment]

            if bot is None:
                # Entity was removed from config
                logger.info(f"Skipping {entity_name} - no longer in configuration")
                continue

            bot.orchestrator = self
            self.agent_bots[entity_name] = bot

            await bot.start()
            asyncio.create_task(bot.sync_forever())

            logger.info(f"Started bot: {entity_name}")

        logger.info(f"Configuration update complete: {len(entities_to_restart)} bots affected")
        return True

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False

        # Signal all bots to stop their sync loops
        for bot in self.agent_bots.values():
            bot.running = False

        # Give time for in-progress messages to complete
        logger.info("Waiting for in-progress messages to complete...")
        await asyncio.sleep(3)

        # Now stop all bots
        stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")

    async def invite_agents_to_room(self, room_id: str, inviter_client: nio.AsyncClient) -> None:
        """Invite all agent users to a room.

        Args:
            room_id: The room to invite agents to
            inviter_client: An authenticated client with invite permissions
        """
        for agent_name, bot in self.agent_bots.items():
            result = await inviter_client.room_invite(room_id, bot.agent_user.user_id)
            if isinstance(result, nio.RoomInviteResponse):
                logger.info("Invited agent", agent=agent_name, room_id=room_id)
            else:
                logger.error("Failed to invite agent", agent=agent_name, error=str(result))


async def main(log_level: str, storage_path: Path) -> None:
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
        storage_path: The base directory for storing agent data
    """
    from watchfiles import awatch

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
        async def watch_config():
            """Watch config file for changes and reload when modified."""
            async for _changes in awatch(config_path):
                # The changes set contains tuples of (change_type, path)
                # We only care that the file changed, not the specific type
                logger.info("Configuration file changed, checking for updates...")

                if orchestrator.running:
                    updated = await orchestrator.update_config()
                    if updated:
                        logger.info("Configuration update applied to affected agents")
                    else:
                        logger.info("No agent changes detected in configuration update")

                # Break if orchestrator is no longer running
                if not orchestrator.running:
                    break

        # Run config watcher in parallel with orchestrator
        watcher_task = asyncio.create_task(watch_config())

        # Wait for either orchestrator or watcher to complete
        done, pending = await asyncio.wait({orchestrator_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED)

        # Cancel any pending tasks
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    except Exception as e:
        logger.error(f"Error in orchestrator: {e}")
    finally:
        # Final cleanup
        if orchestrator is not None:
            await orchestrator.stop()
