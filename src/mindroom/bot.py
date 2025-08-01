"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import nio

from .agent_loader import load_config
from .ai import ai_response
from .logging_config import emoji, get_logger
from .matrix import fetch_thread_history, prepare_response_content
from .matrix_agent_manager import AgentMatrixUser, ensure_all_agent_users, login_agent_user
from .matrix_room_manager import get_room_aliases
from .response_tracker import ResponseTracker

logger = get_logger(__name__)


def is_sender_other_agent(sender: str, current_agent_user_id: str) -> bool:
    """Check if sender is another agent (not the current agent, not the user)."""
    if sender == current_agent_user_id:
        return False

    sender_username = sender.split(":")[0][1:] if sender.startswith("@") else ""
    return sender_username.startswith("mindroom_") and sender_username != "mindroom_user"


def has_other_agents_in_thread(thread_history: list[dict], current_agent_user_id: str) -> bool:
    """Check if other agents have participated in this thread."""
    return any(is_sender_other_agent(msg.get("sender", ""), current_agent_user_id) for msg in thread_history)


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

    async def start(self) -> None:
        """Start the agent bot."""
        try:
            self.client = await login_agent_user(self.agent_user)

            # Initialize response tracker
            self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)

            # Register event callbacks
            logger.debug(f"{emoji(self.agent_name)} Registering event callbacks")
            self.client.add_event_callback(self._on_invite, nio.InviteEvent)
            self.client.add_event_callback(self._on_message, nio.RoomMessageText)
            logger.debug(f"{emoji(self.agent_name)} Event callbacks registered")

            self.running = True
            logger.info(
                f"{emoji(self.agent_name)} Started agent bot: {self.agent_user.display_name} "
                f"({self.agent_user.user_id})"
            )

            # Join configured rooms
            for room_id in self.rooms:
                try:
                    response = await self.client.join(room_id)
                    if isinstance(response, nio.JoinResponse):
                        logger.info(f"{emoji(self.agent_name)} Joined room {room_id}")
                    else:
                        logger.warning(f"{emoji(self.agent_name)} Could not join room {room_id}: {response}")
                except Exception as e:
                    logger.error(f"{emoji(self.agent_name)} Error joining room {room_id}: {e}")

        except Exception as e:
            logger.error(f"{emoji(self.agent_name)} Failed to start: {e}")
            raise

    async def sync_forever(self) -> None:
        """Run the sync loop forever."""
        if not self.client:
            return

        logger.info(f"{emoji(self.agent_name)} Starting sync_forever")
        try:
            await self.client.sync_forever(timeout=30000, full_state=True)
        except Exception as e:
            logger.error(f"{emoji(self.agent_name)} Error in sync_forever: {e}")
            raise

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        if self.client:
            await self.client.close()
        logger.info(f"{emoji(self.agent_name)} Stopped agent bot")

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle room invitations."""
        logger.info(f"{emoji(self.agent_name)} Received invite to room {room.room_id} from {event.sender}")
        if self.client:
            result = await self.client.join(room.room_id)
            if isinstance(result, nio.JoinResponse):
                logger.info(f"{emoji(self.agent_name)} Joined room {room.room_id}")
            else:
                logger.error(f"{emoji(self.agent_name)} Failed to join room {room.room_id}: {result}")

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle messages in rooms."""
        logger.debug(
            f"{emoji(self.agent_name)} Message received",
            room_id=room.room_id,
            room_name=room.display_name,
            sender=event.sender,
            body=event.body,
            event_id=event.event_id,
        )

        # Don't respond to own messages
        if event.sender == self.agent_user.user_id:
            logger.debug(f"{emoji(self.agent_name)} Ignoring own message")
            return

        # Don't respond to other agent messages (avoid agent loops)
        if is_sender_other_agent(event.sender, self.agent_user.user_id):
            logger.debug(f"{emoji(self.agent_name)} Ignoring message from other agent: {event.sender}")
            return

        # Debug logging
        logger.debug(
            f"{emoji(self.agent_name)} Checking message: '{event.body}' - "
            f"Agent user_id: {self.agent_user.user_id}, display_name: {self.agent_user.display_name}"
        )

        # Check if this agent is mentioned
        mentions = event.source.get("content", {}).get("m.mentions", {})
        mentioned = self.agent_user.user_id in mentions.get("user_ids", [])

        # Check if we're in a thread
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
        thread_id = relates_to.get("event_id") if is_thread else None

        # Decide whether to respond
        thread_history = []

        if mentioned:
            # Always respond if explicitly mentioned
            logger.debug(f"{emoji(self.agent_name)} Will respond: explicitly mentioned")
        elif is_thread and thread_id and self.client:
            # In threads, check if we're the only agent participating
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

            if has_other_agents_in_thread(thread_history, self.agent_user.user_id):
                logger.debug(f"{emoji(self.agent_name)} Not responding: multiple agents in thread, no explicit mention")
                return

            logger.debug(f"{emoji(self.agent_name)} Will respond: only agent in thread")
        else:
            logger.debug(f"{emoji(self.agent_name)} Not responding to message")
            return

        # Check if we've already responded to this specific event
        if self.response_tracker.has_responded(event.event_id):
            logger.info(
                f"{emoji(self.agent_name)} Already responded to event {event.event_id} from {event.sender}, skipping"
            )
            return

        logger.info(f"{emoji(self.agent_name)} WILL PROCESS message from {event.sender}: {event.body}")

        # For now, use the full message body as the prompt
        # The actual mention text might not be in the body with modern Matrix clients
        prompt = event.body.strip()

        if not prompt:
            return

        # Create session ID with thread awareness
        session_id = f"{room.room_id}:{thread_id}" if thread_id else room.room_id

        # Fetch thread history if we haven't already
        if is_thread and not thread_history:
            assert thread_id is not None  # is_thread guarantees thread_id exists
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        # Generate response
        response_text = await ai_response(
            agent_name=self.agent_name,
            prompt=prompt,
            session_id=session_id,
            storage_path=self.storage_path,
            thread_history=thread_history,
        )

        # Prepare and send response
        content = prepare_response_content(response_text, event, agent_name=self.agent_name)

        logger.debug(
            f"{emoji(self.agent_name)} Sending response - Room ID: {room.room_id}, "
            f"Message type: m.room.message, Content: {content}"
        )

        if self.client:
            response = await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
            )
            if isinstance(response, nio.RoomSendResponse):
                # Mark this event as responded to
                self.response_tracker.mark_responded(event.event_id)
                logger.info(f"{emoji(self.agent_name)} Sent response to room {room.room_id}")
            else:
                logger.error(f"{emoji(self.agent_name)} Failed to send response: {response}")


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
        agent_users = await ensure_all_agent_users()

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

        logger.info(f"Initialized {len(self.agent_bots)} agent bots")

    async def start(self) -> None:
        """Start all agent bots."""
        if not self.agent_bots:
            await self.initialize()

        # Start each agent bot
        await asyncio.gather(*(bot.start() for bot in self.agent_bots.values()))
        self.running = True
        logger.info("All agent bots started successfully")

        # Run sync loops for all agents concurrently
        await asyncio.gather(*(bot.sync_forever() for bot in self.agent_bots.values()))

    async def stop(self) -> None:
        """Stop all agent bots."""
        self.running = False
        stop_tasks = []
        for bot in self.agent_bots.values():
            stop_tasks.append(bot.stop())

        await asyncio.gather(*stop_tasks)
        logger.info("All agent bots stopped")

    async def invite_agents_to_room(self, room_id: str, inviter_client: nio.AsyncClient) -> None:
        """Invite all agent users to a room.

        Args:
            room_id: The room to invite agents to
            inviter_client: An authenticated client with invite permissions
        """
        for agent_name, bot in self.agent_bots.items():
            try:
                await inviter_client.room_invite(room_id, bot.agent_user.user_id)
                logger.info(f"Invited agent {agent_name} to room {room_id}")
            except Exception as e:
                logger.error(f"Failed to invite agent {agent_name} to room {room_id}: {e}")


async def main(log_level: str, storage_path: Path) -> None:
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
        storage_path: The base directory for storing agent data
    """
    from .logging_config import setup_logging

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
