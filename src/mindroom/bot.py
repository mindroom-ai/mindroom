"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
import os
from dataclasses import dataclass, field

import nio
from loguru import logger

from .agent_loader import load_config
from .ai import ai_response
from .logging_config import colorize
from .matrix import fetch_thread_history, prepare_response_content
from .matrix_agent_manager import AgentMatrixUser, ensure_all_agent_users, login_agent_user
from .matrix_room_manager import get_room_aliases

# Enable colors in logger
logger = logger.opt(colors=True)


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    agent_user: AgentMatrixUser
    rooms: list[str] = field(default_factory=list)
    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)

    @property
    def agent_name(self) -> str:
        """Get the agent name from the user."""
        return self.agent_user.agent_name

    async def start(self) -> None:
        """Start the agent bot."""
        try:
            self.client = await login_agent_user(self.agent_user)

            # Register event callbacks
            logger.debug(f"{colorize(self.agent_name)} Registering event callbacks")
            self.client.add_event_callback(self._on_invite, nio.InviteEvent)
            self.client.add_event_callback(self._on_message, nio.RoomMessageText)
            logger.debug(f"{colorize(self.agent_name)} Event callbacks registered")

            self.running = True
            logger.info(
                f"{colorize(self.agent_name)} Started agent bot: {self.agent_user.display_name} ({self.agent_user.user_id})"
            )

            # Auto-join configured rooms
            if self.rooms:
                await self._join_configured_rooms()
        except Exception as e:
            logger.error(f"Failed to start agent bot {self.agent_name}: {e}")
            raise

    async def _join_configured_rooms(self) -> None:
        """Join all configured rooms for this agent."""
        if not self.client:
            return

        for room_id in self.rooms:
            try:
                # Try to join the room
                response = await self.client.join(room_id)
                if isinstance(response, nio.JoinResponse):
                    logger.info(f"{colorize(self.agent_name)} Joined room: {room_id}")
                else:
                    logger.warning(f"{colorize(self.agent_name)} Could not join room {room_id}: {response}")
            except Exception as e:
                logger.error(f"{colorize(self.agent_name)} Error joining room {room_id}: {e}")

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        if not self.client or not self.running:
            logger.warning(
                f"{colorize(self.agent_name)} Cannot sync: client={self.client is not None}, running={self.running}"
            )
            return

        logger.info(f"{colorize(self.agent_name)} Starting sync_forever")
        try:
            # Use full_state=True to work with stored sync tokens
            # This ensures we don't reprocess old messages after restart
            # The library will automatically use timeout=0 for the first sync
            await self.client.sync_forever(timeout=30000, full_state=True)
        except Exception as e:
            logger.error(f"{colorize(self.agent_name)} Sync error: {e}")
            self.running = False

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        if self.client:
            await self.client.close()
            logger.info(f"{colorize(self.agent_name)} Stopped agent bot: {self.agent_user.display_name}")

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle room invitations."""
        logger.info(f"{colorize(self.agent_name)} Received invite to room: {room.display_name} ({room.room_id})")
        if self.client:
            await self.client.join(room.room_id)
            logger.info(f"{colorize(self.agent_name)} Joined room: {room.room_id}")

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle messages in rooms."""
        logger.debug(
            f"{colorize(self.agent_name)} Message received - Room: {room.room_id} ({room.display_name}), "
            f"Sender: {event.sender}, Body: '{event.body}', Event source: {event.source}"
        )

        # Don't respond to own messages
        if event.sender == self.agent_user.user_id:
            logger.debug(f"{colorize(self.agent_name)} Ignoring own message")
            return

        # Don't respond to other agent messages (avoid agent loops)
        # Extract username from sender ID (e.g., @mindroom_calculator:localhost -> mindroom_calculator)
        sender_username = event.sender.split(":")[0][1:]  # Remove @ and domain

        # Check if sender is another agent (but not the user account)
        if (
            sender_username.startswith("mindroom_")
            and sender_username != "mindroom_user"  # Allow user messages
            and event.sender != self.agent_user.user_id
        ):  # Allow own messages (already filtered above)
            logger.debug(f"{colorize(self.agent_name)} Ignoring message from other agent: {event.sender}")
            return

        # Debug logging
        logger.debug(
            f"{colorize(self.agent_name)} Checking message: '{event.body}' - "
            f"Agent user_id: {self.agent_user.user_id}, display_name: {self.agent_user.display_name}"
        )

        # Check if this agent is mentioned using the proper Matrix m.mentions field
        mentions = event.source.get("content", {}).get("m.mentions", {})
        mentioned_users = mentions.get("user_ids", [])
        mentioned = self.agent_user.user_id in mentioned_users

        logger.debug(
            f"{colorize(self.agent_name)} Checking mentions - m.mentions field: {mentions}, Agent is mentioned: {mentioned}"
        )

        if not mentioned:
            # In threads, respond to all messages
            relates_to = event.source.get("content", {}).get("m.relates_to", {})
            is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
            logger.debug(f"{colorize(self.agent_name)} Thread check - is_thread: {is_thread}, relates_to: {relates_to}")
            if not is_thread:
                logger.debug(f"{colorize(self.agent_name)} Not mentioned and not in thread, ignoring message")
                return

        logger.info(f"{colorize(self.agent_name)} WILL PROCESS message from {event.sender}: {event.body}")

        # For now, use the full message body as the prompt
        # The actual mention text might not be in the body with modern Matrix clients
        prompt = event.body.strip()

        if not prompt:
            return

        # Create session ID with thread awareness
        thread_id = None
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        if relates_to and relates_to.get("rel_type") == "m.thread":
            thread_id = relates_to.get("event_id")

        session_id = f"{room.room_id}:{thread_id}" if thread_id else room.room_id

        # Fetch thread history if in a thread
        thread_history = []
        if thread_id and self.client:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        # Generate response
        response_text = await ai_response(self.agent_name, prompt, session_id, thread_history=thread_history)

        # Prepare and send response
        content = prepare_response_content(response_text, event, agent_name=self.agent_name)

        logger.debug(
            f"{colorize(self.agent_name)} Sending response - Room ID: {room.room_id}, "
            f"Message type: m.room.message, Content: {content}"
        )

        if self.client:
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
            )
            logger.info(f"{colorize(self.agent_name)} Sent response to room {room.room_id}")


@dataclass
class MultiAgentOrchestrator:
    """Orchestrates multiple agent bots."""

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

            bot = AgentBot(agent_user, rooms=resolved_rooms)
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


async def main(log_level: str = "INFO") -> None:
    """Main entry point for the multi-agent bot system.

    Args:
        log_level: The logging level to use (DEBUG, INFO, WARNING, ERROR)
    """
    from .logging_config import setup_logging

    # Set up logging with the specified level
    setup_logging(level=log_level)

    # Create tmp directory for sqlite dbs if it doesn't exist
    if not os.path.exists("tmp"):
        os.makedirs("tmp")

    orchestrator = MultiAgentOrchestrator()
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Multi-agent bot system stopped by user")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
