"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
import os
from dataclasses import dataclass, field

import nio
from loguru import logger

from .agent_loader import load_config
from .ai import ai_response
from .logging_config import setup_logging
from .matrix import fetch_thread_history, prepare_response_content
from .matrix_agent_manager import AgentMatrixUser, ensure_all_agent_users, login_agent_user
from .matrix_room_manager import get_room_aliases

setup_logging(level="INFO")


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
            self.client.add_event_callback(self._on_invite, nio.InviteEvent)
            self.client.add_event_callback(self._on_message, nio.RoomMessageText)
            self.running = True
            logger.info(f"Started agent bot: {self.agent_user.display_name} ({self.agent_user.user_id})")

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
                    logger.info(f"Agent {self.agent_name} joined room: {room_id}")
                else:
                    logger.warning(f"Agent {self.agent_name} could not join room {room_id}: {response}")
            except Exception as e:
                logger.error(f"Error joining room {room_id} for agent {self.agent_name}: {e}")

    async def sync_forever(self) -> None:
        """Run the sync loop for this agent."""
        if not self.client or not self.running:
            return

        try:
            await self.client.sync_forever(timeout=30000)
        except Exception as e:
            logger.error(f"Sync error for agent {self.agent_name}: {e}")
            self.running = False

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        if self.client:
            await self.client.close()
            logger.info(f"Stopped agent bot: {self.agent_user.display_name}")

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle room invitations."""
        logger.info(f"Agent {self.agent_name} received invite to room: {room.display_name} ({room.room_id})")
        if self.client:
            await self.client.join(room.room_id)
            logger.info(f"Agent {self.agent_name} joined room: {room.room_id}")

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle messages in rooms."""
        # Don't respond to own messages
        if event.sender == self.agent_user.user_id:
            return

        # Don't respond to other agent messages (avoid agent loops)
        if event.sender.startswith("@mindroom_") and event.sender != self.agent_user.user_id:
            return

        # Check if this agent is mentioned (with @ symbol)
        mentioned = (
            f"@{self.agent_user.user_id}" in event.body  # Full Matrix ID mention
            or f"@{self.agent_user.display_name}" in event.body  # Display name mention
            or self.agent_user.user_id in event.body  # Direct user ID (Matrix sometimes includes without @)
        )
        if not mentioned:
            # In threads, respond to all messages
            relates_to = event.source.get("content", {}).get("m.relates_to", {})
            is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
            if not is_thread:
                return

        logger.info(f"Agent {self.agent_name} processing message from {event.sender}: {event.body}")

        # Extract prompt (remove agent mention)
        prompt = event.body
        # Remove various mention formats
        mentions_to_remove = [
            f"@{self.agent_user.user_id}",  # Full Matrix ID with @
            f"@{self.agent_user.display_name}",  # Display name with @
            self.agent_user.user_id,  # Just the user ID
            self.agent_user.display_name,  # Just the display name
        ]
        for mention in mentions_to_remove:
            prompt = prompt.replace(mention, "").strip()
        prompt = prompt.lstrip(":").strip()

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
        content = prepare_response_content(response_text, event)

        if self.client:
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
            )
            logger.info(f"Agent {self.agent_name} sent response to room {room.room_id}")


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


async def main() -> None:
    """Main entry point for the multi-agent bot system."""
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
