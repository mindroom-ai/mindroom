"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import nio

from .agent_loader import load_config
from .ai import ai_response
from .commands import Command, CommandType, command_parser, get_command_help
from .logging_config import emoji, get_logger, setup_logging
from .matrix import (
    MATRIX_HOMESERVER,
    AgentMatrixUser,
    construct_agent_user_id,
    create_mention_content_from_text,
    ensure_all_agent_users,
    extract_agent_name,
    extract_domain_from_user_id,
    extract_thread_info,
    fetch_thread_history,
    get_room_aliases,
    login_agent_user,
)
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .thread_invites import thread_invite_manager
from .thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_available_agents_in_room,
    has_any_agent_mentions_in_thread,
    has_room_access,
    should_agent_respond,
    should_route_to_agent,
)

logger = get_logger(__name__)


async def _handle_invite_command(
    room_id: str,
    thread_id: str,
    agent_name: str,
    duration_hours: int | None,
    sender: str,
    agent_domain: str,
    client: nio.AsyncClient | None,
) -> str:
    """Handle the invite command logic."""
    # Check if agent exists
    config = load_config()
    if agent_name not in config.agents:
        return f"❌ Unknown agent: {agent_name}. Available agents: {', '.join(config.agents.keys())}"

    # Add the invitation
    await thread_invite_manager.add_invite(
        thread_id=thread_id,
        room_id=room_id,
        agent_name=agent_name,
        invited_by=sender,
        duration_hours=duration_hours,
    )

    # Also invite to Matrix room if needed (for Matrix protocol compliance)
    agent_user_id = construct_agent_user_id(agent_name, agent_domain)
    try:
        assert client is not None, "Client should always be available"
        # Check if already in room
        room_members = await client.joined_members(room_id)
        if isinstance(room_members, nio.JoinedMembersResponse) and agent_user_id not in [
            m.user_id for m in room_members.members
        ]:
            # Need to invite to room for Matrix compliance
            result = await client.room_invite(room_id, agent_user_id)
            if not isinstance(result, nio.RoomInviteResponse):
                logger.warning(f"Failed to invite {agent_name} to Matrix room: {result}")
    except Exception as e:
        logger.warning(f"Error checking/inviting {agent_name} to Matrix room: {e}")

    duration_text = f" for {duration_hours} hours" if duration_hours else " until thread ends"
    response_text = f"✅ Invited @{agent_name} to this thread{duration_text}."
    # Mention the agent so they know they're invited
    response_text += f"\n\n@{agent_name}, you've been invited to help in this thread!"
    return response_text


async def _handle_list_invites_command(room_id: str, thread_id: str) -> str:
    """Handle the list invites command."""
    # Get thread invites
    thread_invites = await thread_invite_manager.get_thread_agents(thread_id)
    if thread_invites:
        thread_list = "\n".join([f"- @{agent}" for agent in thread_invites])
        return f"**Invited agents in this thread:**\n{thread_list}"

    return "No agents are currently invited to this thread."


def _is_sender_other_agent(sender: str, current_agent_user_id: str) -> bool:
    if sender == current_agent_user_id:
        return False

    # Use existing extract_agent_name function which returns None for non-agents
    return extract_agent_name(sender) is not None


def _should_process_message(event_sender: str, agent_user_id: str) -> bool:
    # Don't respond to own messages
    if event_sender == agent_user_id:
        return False

    # Don't respond to other agent messages (avoid agent loops)
    return not _is_sender_other_agent(event_sender, agent_user_id)


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    agent_user: AgentMatrixUser
    storage_path: Path
    rooms: list[str] = field(default_factory=list)
    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    response_tracker: ResponseTracker = field(init=False)

    @property
    def agent_name(self) -> str:
        """Get the agent name from username."""
        return self.agent_user.agent_name

    @cached_property
    def logger(self):
        """Get a logger with agent context bound."""
        return logger.bind(agent=f"{emoji(self.agent_name)} {self.agent_name}")

    async def start(self) -> None:
        """Start the agent bot."""
        try:
            self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)

            # Initialize response tracker
            self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)

            # Register event callbacks
            self.logger.debug("Registering event callbacks")
            self.client.add_event_callback(self._on_invite, nio.InviteEvent)
            self.client.add_event_callback(self._on_message, nio.RoomMessageText)
            self.logger.debug("Event callbacks registered")

            self.running = True
            self.logger.info(
                "Started agent bot",
                display_name=self.agent_user.display_name,
                user_id=self.agent_user.user_id,
            )

            # Join configured rooms
            for room_id in self.rooms:
                try:
                    response = await self.client.join(room_id)
                    if isinstance(response, nio.JoinResponse):
                        self.logger.info("Joined room", room_id=room_id)
                    else:
                        self.logger.warning(
                            "Could not join room",
                            room_id=room_id,
                            error=str(response),
                        )
                except Exception as e:
                    self.logger.error(
                        "Error joining room",
                        room_id=room_id,
                        error=str(e),
                    )

        except Exception as e:
            self.logger.error("Failed to start", error=str(e))
            raise

    async def sync_forever(self) -> None:
        """Run the sync loop forever."""
        self.logger.info("Starting sync_forever")
        try:
            assert self.client is not None, "Client should be initialized"
            await self.client.sync_forever(timeout=30000, full_state=True)
        except Exception as e:
            self.logger.error("Error in sync_forever", error=str(e))
            raise

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        if self.client:
            await self.client.close()
        self.logger.info("Stopped agent bot")

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        self.logger.info(
            "Received invite to room",
            room_id=room.room_id,
            sender=event.sender,
        )
        assert self.client is not None, "Client should be initialized"
        result = await self.client.join(room.room_id)
        if isinstance(result, nio.JoinResponse):
            self.logger.info("Joined room", room_id=room.room_id)
        else:
            self.logger.error(
                "Failed to join room",
                room_id=room.room_id,
                error=str(result),
            )

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        self.logger.debug(
            "Message received",
            room_id=room.room_id,
            room_name=room.display_name,
            sender=event.sender,
            body=event.body,
            event_id=event.event_id,
        )

        # Validate message sender
        if not await self._should_process_message(event):
            return

        # Check room permissions
        if not await self._has_room_access(room.room_id):
            return

        # Handle commands (only general agent)
        if await self._try_handle_command(room, event):
            return

        # Extract message context
        context = await self._extract_message_context(room, event)

        # Determine if this agent should respond to the message
        should_respond, use_router = await self._should_respond_to_message(
            am_i_mentioned=context["am_i_mentioned"],
            is_thread=context["is_thread"],
            is_invited_to_thread=context["is_invited_to_thread"],
            thread_history=context["thread_history"],
            room_id=room.room_id,
        )

        # Handle routing if needed
        if use_router:
            await self._handle_ai_routing(room, event, context["thread_history"])
            return

        # Exit if not responding
        if not should_respond:
            return

        # Check if we've already responded to this specific event
        if self.response_tracker.has_responded(event.event_id):
            logger.info(
                "Already responded to event, skipping",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                event_id=event.event_id,
                sender=event.sender,
            )
            return

        # Process and send response
        await self._process_and_respond(room, event, context["thread_id"], context["thread_history"])

    async def _should_respond_to_message(
        self,
        am_i_mentioned: bool,
        is_thread: bool,
        is_invited_to_thread: bool,
        thread_history: list[dict],
        room_id: str,
    ) -> tuple[bool, bool]:
        decision = should_agent_respond(
            self.agent_name,
            am_i_mentioned,
            is_thread,
            is_invited_to_thread,
            room_id,
            self.rooms,
            thread_history,
        )

        # Log decision
        if decision.should_respond:
            if am_i_mentioned:
                self.logger.debug("Will respond: explicitly mentioned")
            else:
                self.logger.debug("Will respond: only agent in thread")
        elif decision.use_router:
            logger.debug(
                "Not responding: no agents yet, will use router",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
            )
        elif is_thread:
            if has_any_agent_mentions_in_thread(thread_history):
                logger.debug(
                    "Not responding: other agents mentioned in thread",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                )
            else:
                agents_in_thread = get_agents_in_thread(thread_history)
                logger.debug(
                    "Not responding: multiple agents in thread, need explicit mention",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    agents_in_thread=agents_in_thread,
                )
        else:
            logger.debug(
                "Not responding: not in thread or mentioned", agent=f"{emoji(self.agent_name)} {self.agent_name}"
            )

        return decision.should_respond, decision.use_router

    async def _should_process_message(self, event: nio.RoomMessageText) -> bool:
        should_process = _should_process_message(event.sender, self.agent_user.user_id)

        if not should_process:
            if event.sender == self.agent_user.user_id:
                self.logger.debug("Ignoring own message")
            else:
                logger.debug(
                    "Ignoring message from other agent",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    sender=event.sender,
                )

        return should_process

    async def _has_room_access(self, room_id: str) -> bool:
        has_access = await has_room_access(room_id, self.agent_name, self.rooms)
        if not has_access:
            logger.debug(
                "Not in this room and not invited",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                room_id=room_id,
            )
        return has_access

    async def _try_handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> bool:
        if self.agent_name == "general":
            command = command_parser.parse(event.body)
            if command:
                await self._handle_command(room, event, command)
                return True
        return False

    async def _extract_message_context(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> dict:
        logger.debug(
            "Checking message",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            body=event.body,
            user_id=self.agent_user.user_id,
            display_name=self.agent_user.display_name,
        )

        # Extract mentions
        mentioned_agents, am_i_mentioned = check_agent_mentioned(event.source, self.agent_name)

        # Log mention detection
        if mentioned_agents:
            logger.debug(
                "Detected mentions",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                mentioned_agents=mentioned_agents,
            )
        if am_i_mentioned:
            logger.info(
                "I am mentioned in message",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                body_preview=event.body[:100],
                event_id=event.event_id,
            )

        # Extract thread info
        is_thread, thread_id = extract_thread_info(event.source)

        # Fetch thread history if in thread
        thread_history = []
        if thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        # Check if I'm invited to this thread
        is_invited_to_thread = False
        if thread_id:
            is_invited_to_thread = await thread_invite_manager.is_agent_invited_to_thread(thread_id, self.agent_name)
            if is_invited_to_thread:
                logger.debug(
                    "Agent is invited to this thread",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    thread_id=thread_id,
                )

        return {
            "am_i_mentioned": am_i_mentioned,
            "is_thread": is_thread,
            "thread_id": thread_id,
            "thread_history": thread_history,
            "is_invited_to_thread": is_invited_to_thread,
        }

    async def _process_and_respond(
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_id: str | None, thread_history: list[dict]
    ) -> None:
        logger.info(
            "WILL PROCESS message",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            sender=event.sender,
            body=event.body,
            event_id=event.event_id,
        )

        # Extract prompt
        prompt = event.body.strip()
        if not prompt:
            return

        # Create session ID with thread awareness
        session_id = create_session_id(room.room_id, thread_id)

        # Generate response
        response_text = await ai_response(
            agent_name=self.agent_name,
            prompt=prompt,
            session_id=session_id,
            storage_path=self.storage_path,
            thread_history=thread_history,
            room_id=room.room_id,
        )

        # Send response
        await self._send_response(room.room_id, event.event_id, response_text, thread_id)

    async def _send_response(
        self, room_id: str, reply_to_event_id: str, response_text: str, thread_id: str | None = None
    ) -> None:
        # Extract domain from agent's user_id
        sender_domain = extract_domain_from_user_id(self.agent_user.user_id)

        # Parse response for any agent mentions
        content = create_mention_content_from_text(
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
            reply_to_event_id=reply_to_event_id if thread_id else None,
        )

        logger.debug(
            "Sending response",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        assert self.client is not None, "Client should be initialized"
        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
        if isinstance(response, nio.RoomSendResponse):
            # Mark this event as responded to
            self.response_tracker.mark_responded(reply_to_event_id)
            logger.info(
                "Sent response to room",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                room_id=room_id,
                response_event_id=response.event_id,
            )
        else:
            logger.error(
                "Failed to send response",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                error=str(response),
            )

    async def _handle_ai_routing(
        self, room: nio.MatrixRoom, event: nio.RoomMessageText, thread_history: list[dict]
    ) -> None:
        # Only let one agent do the routing to avoid duplicates
        available_agents = get_available_agents_in_room(room)
        if not should_route_to_agent(self.agent_name, available_agents):
            return  # Let another agent handle routing

        logger.info(
            "Handling AI routing",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            body_preview=event.body[:50],
            event_id=event.event_id,
        )

        # Get thread info if available
        _, thread_event_id = extract_thread_info(event.source)

        # Get AI suggestion (including invited agents)
        suggested_agent = await suggest_agent_for_message(event.body, available_agents, thread_history, thread_event_id)
        if not suggested_agent:
            return

        # Send mention to suggested agent
        response_text = "could you help with this?"

        # Use universal mention parser
        sender_domain = extract_domain_from_user_id(self.agent_user.user_id)
        full_message = f"@{suggested_agent} {response_text}"

        content = create_mention_content_from_text(
            full_message,
            sender_domain=sender_domain,
            thread_event_id=thread_event_id,
            reply_to_event_id=event.event_id,
        )

        assert self.client is not None, "Client should be initialized"
        await self.client.room_send(room_id=room.room_id, message_type="m.room.message", content=content)
        logger.info(
            "Routed to agent",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            suggested_agent=suggested_agent,
            room_id=room.room_id,
        )

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:
        logger.info(
            "Handling command",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            command_type=command.type.value,
            args=command.args,
        )

        # Get thread info
        is_thread, thread_id = extract_thread_info(event.source)

        # Commands only work in threads
        if not is_thread or not thread_id:
            response_text = "❌ Commands only work within threads. Please start a thread first."
            await self._send_response(room, event, response_text)
            return

        response_text = ""

        if command.type == CommandType.INVITE:
            # Handle invite command
            agent_name = command.args["agent_name"]
            duration_hours = command.args.get("duration_hours")
            agent_domain = extract_domain_from_user_id(self.agent_user.user_id)

            response_text = await _handle_invite_command(
                room_id=room.room_id,
                thread_id=thread_id,
                agent_name=agent_name,
                duration_hours=duration_hours,
                sender=event.sender,
                agent_domain=agent_domain,
                client=self.client,
            )

        elif command.type == CommandType.UNINVITE:
            # Handle uninvite command
            if not thread_id:
                response_text = "❌ The /uninvite command can only be used in a thread."
            else:
                agent_name = command.args["agent_name"]
                removed = await thread_invite_manager.remove_invite(thread_id, agent_name)
                if removed:
                    response_text = f"✅ Removed @{agent_name} from this thread."
                else:
                    response_text = f"❌ @{agent_name} was not invited to this thread."

        elif command.type == CommandType.LIST_INVITES:
            # Handle list invites command
            response_text = await _handle_list_invites_command(room.room_id, thread_id)

        elif command.type == CommandType.HELP:
            # Handle help command
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        # Send response
        if response_text:
            sender_domain = extract_domain_from_user_id(self.agent_user.user_id)
            content = create_mention_content_from_text(
                response_text,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
                reply_to_event_id=event.event_id if thread_id else None,
            )

            assert self.client is not None, "Client should be initialized"
            response = await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
            )
            if isinstance(response, nio.RoomSendResponse):
                logger.info(
                    "Sent command response",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    command_type=command.type.value,
                )


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

    storage_path: Path
    agent_bots: dict[str, AgentBot] = field(default_factory=dict, init=False)
    running: bool = field(default=False, init=False)

    async def initialize(self) -> None:
        """Initialize all agent bots."""
        logger.info("Initializing multi-agent system...")

        # Load agent configuration
        config = load_config()

        # Ensure all agents have Matrix accounts
        agent_users = await ensure_all_agent_users(MATRIX_HOMESERVER)

        # Get room aliases mapping from matrix_rooms.yaml
        room_aliases = get_room_aliases()

        # Create bot instances for each agent
        for agent_name, agent_user in agent_users.items():
            # Get rooms from agent configuration
            agent_config = config.agents.get(agent_name)
            rooms = agent_config.rooms if agent_config else []

            # Resolve room aliases to actual room IDs
            resolved_rooms = []
            for room in rooms:
                # If it's an alias, resolve it; otherwise use as-is
                resolved_room = room_aliases.get(room, room)
                resolved_rooms.append(resolved_room)

            bot = AgentBot(agent_user, self.storage_path, rooms=resolved_rooms)
            self.agent_bots[agent_name] = bot

        logger.info("Initialized agent bots", count=len(self.agent_bots))

    async def start(self) -> None:
        """Start all agent bots."""
        if not self.agent_bots:
            await self.initialize()

        # Start each agent bot
        start_tasks = [bot.start() for bot in self.agent_bots.values()]
        await asyncio.gather(*start_tasks)
        self.running = True
        logger.info("All agent bots started successfully")

        # Create cleanup task for expired invitations
        cleanup_task = asyncio.create_task(self._periodic_cleanup())

        # Run sync loops for all agents concurrently
        sync_tasks = [bot.sync_forever() for bot in self.agent_bots.values()]

        # Run all tasks together
        all_tasks = sync_tasks + [cleanup_task]
        await asyncio.gather(*all_tasks)

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        stop_tasks = [bot.stop() for bot in self.agent_bots.values()]
        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")

    async def _periodic_cleanup(self) -> None:
        logger.info("Starting periodic cleanup task")

        while self.running:
            try:
                # Wait for 1 minute between checks
                await asyncio.sleep(60)  # 1 minute

                # Clean up expired thread invitations
                thread_removed = await thread_invite_manager.cleanup_expired()
                if thread_removed > 0:
                    logger.info(f"Cleaned up {thread_removed} expired thread invitations")

            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
                # Continue running even if cleanup fails

    async def invite_agents_to_room(self, room_id: str, inviter_client: nio.AsyncClient) -> None:
        """Invite all agent users to a room.

        Args:
            room_id: The room to invite agents to
            inviter_client: An authenticated client with invite permissions
        """
        for agent_name, bot in self.agent_bots.items():
            try:
                await inviter_client.room_invite(room_id, bot.agent_user.user_id)
                logger.info("Invited agent to room", agent=agent_name, room_id=room_id)
            except Exception as e:
                logger.error("Failed to invite agent to room", agent=agent_name, room_id=room_id, error=str(e))


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
