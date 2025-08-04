"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import nio

from .agent_config import load_config
from .ai import ai_response, ai_response_streaming
from .commands import (
    Command,
    CommandType,
    command_parser,
    get_command_help,
    handle_invite_command,
    handle_list_invites_command,
)
from .logging_config import emoji, get_logger, setup_logging
from .matrix import (
    MATRIX_HOMESERVER,
    AgentMatrixUser,
    MatrixID,
    create_mention_content_from_text,
    ensure_all_agent_users,
    extract_agent_name,
    extract_thread_info,
    fetch_thread_history,
    get_room_aliases,
    join_room,
    login_agent_user,
)
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .streaming import StreamingResponse
from .thread_invites import ThreadInviteManager
from .thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_available_agents_in_room,
    should_agent_respond,
    should_route_to_agent,
)

logger = get_logger(__name__)

# Constants
SYNC_TIMEOUT_MS = 30000
CLEANUP_INTERVAL_SECONDS = 3600


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

    @property
    def agent_name(self) -> str:
        """Get the agent name from username."""
        return self.agent_user.agent_name

    @cached_property
    def logger(self):
        """Get a logger with agent context bound."""
        return logger.bind(agent=f"{emoji(self.agent_name)} {self.agent_name}")

    @cached_property
    def matrix_id(self) -> MatrixID:
        """Get the Matrix ID for this agent bot."""
        return MatrixID.parse(self.agent_user.user_id)

    async def start(self) -> None:
        """Start the agent bot."""
        self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)

        # Initialize response tracker
        self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)

        # Initialize thread invite manager
        self.thread_invite_manager = ThreadInviteManager(self.client)

        self.client.add_event_callback(self._on_invite, nio.InviteEvent)
        self.client.add_event_callback(self._on_message, nio.RoomMessageText)

        self.running = True
        self.logger.info("Started bot", user_id=self.agent_user.user_id)

        # Join configured rooms
        for room_id in self.rooms:
            if await join_room(self.client, room_id):
                self.logger.info("Joined room", room_id=room_id)
            else:
                self.logger.warning("Failed to join room", room_id=room_id)

        # Start periodic cleanup task for the general agent only
        if self.agent_name == "general":
            asyncio.create_task(self._periodic_cleanup())

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        await self.client.close()
        self.logger.info("Stopped agent bot")

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
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

        sender_id = MatrixID.parse(event.sender)

        if sender_id.is_agent and sender_id.agent_name:
            await self.thread_invite_manager.update_agent_activity(room.room_id, sender_id.agent_name)

        # Handle commands (only first agent alphabetically to avoid duplicates)
        available_agents = get_available_agents_in_room(room)
        if should_route_to_agent(self.agent_name, available_agents):
            command = command_parser.parse(event.body)
            if command:
                await self._handle_command(room, event, command)
                return

        # Extract message context
        context = await self._extract_message_context(room, event)

        # If message is from another agent and we're not mentioned, ignore it
        sender_is_agent = extract_agent_name(event.sender) is not None
        if sender_is_agent and not context.am_i_mentioned:
            self.logger.debug("Ignoring message from other agent (not mentioned)")
            return

        if sender_is_agent and context.am_i_mentioned and not event.body.rstrip().endswith("✓"):
            self.logger.debug("Ignoring mention from agent - streaming not complete", sender=event.sender)
            return

        # Determine if this agent should respond to the message
        decision = should_agent_respond(
            self.agent_name,
            context.am_i_mentioned,
            context.is_thread,
            context.is_invited_to_thread,
            room.room_id,
            self.rooms,
            context.thread_history,
            context.mentioned_agents,
        )

        if decision.should_respond and not context.am_i_mentioned:
            self.logger.info("Will respond: only agent in thread")

        # Handle routing if needed
        if decision.use_router:
            await self._handle_ai_routing(room, event, context.thread_history)
            return

        if not decision.should_respond:
            return

        if self._should_skip_duplicate_response(event):
            return

        # Process and send response
        if self.enable_streaming:
            await self._process_and_respond_streaming(room, event, context.thread_id, context.thread_history)
        else:
            await self._process_and_respond(room, event, context.thread_id, context.thread_history)

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> MessageContext:
        mentioned_agents, am_i_mentioned = check_agent_mentioned(event.source, self.agent_name)

        if am_i_mentioned:
            self.logger.info("Mentioned", event_id=event.event_id)

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
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_id: str | None, thread_history: list[dict]
    ) -> None:
        self.logger.info("Processing", event_id=event.event_id)

        prompt = event.body.strip()
        if not prompt:
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

        await self._send_response(room.room_id, event.event_id, response_text, thread_id)

    async def _process_and_respond_streaming(
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_id: str | None, thread_history: list[dict]
    ) -> None:
        self.logger.info("Processing streaming", event_id=event.event_id)

        prompt = event.body.strip()
        if not prompt:
            return

        session_id = create_session_id(room.room_id, thread_id)
        sender_id = self.matrix_id

        streaming = StreamingResponse(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            thread_id=thread_id,
            sender_domain=sender_id.domain,
        )

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
                self.response_tracker.mark_responded(event.event_id)
                self.logger.info("Sent streaming response", event_id=streaming.event_id)

        except Exception as e:
            self.logger.error("Error in streaming response", error=str(e))

    async def _send_response(
        self, room_id: str, reply_to_event_id: str, response_text: str, thread_id: str | None
    ) -> bool:
        """Send a response message to a room.

        Returns:
            True if message was sent successfully, False otherwise.
        """
        sender_id = self.matrix_id
        sender_domain = sender_id.domain

        # Always ensure we have a thread_id - use the original message as thread root if needed
        effective_thread_id = thread_id if thread_id else reply_to_event_id

        if not response_text.rstrip().endswith("✓"):
            response_text += " ✓"

        content = create_mention_content_from_text(
            response_text,
            sender_domain=sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=reply_to_event_id,
        )

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
        if isinstance(response, nio.RoomSendResponse):
            self.response_tracker.mark_responded(reply_to_event_id)
            self.logger.info("Sent response", event_id=response.event_id)
            return True
        else:
            self.logger.error("Failed to send response", error=str(response))
            return False

    async def _handle_ai_routing(
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_history: list[dict]
    ) -> None:
        available_agents = get_available_agents_in_room(room)
        if not should_route_to_agent(self.agent_name, available_agents):
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

        response_text = "could you help with this?"
        sender_id = self.matrix_id
        sender_domain = sender_id.domain
        full_message = f"@{suggested_agent} {response_text} ✓"

        # If no thread exists, create one with the original message as root
        if not thread_event_id:
            thread_event_id = event.event_id

        content = create_mention_content_from_text(
            full_message,
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
        if not is_thread or not thread_id:
            response_text = "❌ Commands only work within threads. Please start a thread first."
            # Create a thread even for this error message
            await self._send_response(room.room_id, event.event_id, response_text, thread_id=None)
            return

        response_text = ""

        if command.type == CommandType.INVITE:
            # Handle invite command
            agent_name = command.args["agent_name"]
            agent_domain = self.matrix_id.domain

            response_text = await handle_invite_command(
                room_id=room.room_id,
                thread_id=thread_id,
                agent_name=agent_name,
                sender=event.sender,
                agent_domain=agent_domain,
                client=self.client,
                thread_invite_manager=self.thread_invite_manager,
            )

        elif command.type == CommandType.UNINVITE:
            agent_name = command.args["agent_name"]
            removed = await self.thread_invite_manager.remove_invite(thread_id, room.room_id, agent_name)
            if removed:
                response_text = f"✅ Removed @{agent_name} from this thread."
            else:
                response_text = f"❌ @{agent_name} was not invited to this thread."

        elif command.type == CommandType.LIST_INVITES:
            response_text = await handle_list_invites_command(room.room_id, thread_id, self.thread_invite_manager)

        elif command.type == CommandType.HELP:
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        if response_text:
            await self._send_response(room.room_id, event.event_id, response_text, thread_id)

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
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)

    async def initialize(self) -> None:
        """Initialize all agent bots."""
        logger.info("Initializing multi-agent system...")

        config = load_config()
        agent_users = await ensure_all_agent_users(MATRIX_HOMESERVER)
        room_aliases = get_room_aliases()

        for agent_name, agent_user in agent_users.items():
            agent_config = config.agents.get(agent_name)
            rooms = agent_config.rooms if agent_config else []

            resolved_rooms = []
            for room in rooms:
                resolved_room = room_aliases.get(room, room)
                resolved_rooms.append(resolved_room)

            enable_streaming = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"

            bot = AgentBot(agent_user, self.storage_path, rooms=resolved_rooms, enable_streaming=enable_streaming)
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

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
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
    # Set up logging with the specified level
    setup_logging(level=log_level)

    # Create storage directory if it doesn't exist
    storage_path.mkdir(parents=True, exist_ok=True)

    orchestrator = MultiAgentOrchestrator(storage_path=storage_path)
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    finally:
        await orchestrator.stop()
