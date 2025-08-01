"""Multi-agent bot implementation where each agent has its own Matrix user account."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import nio

from .agent_loader import load_config
from .ai import ai_response
from .commands import Command, CommandType, command_parser, get_command_help
from .logging_config import emoji, get_logger, setup_logging
from .matrix import (
    MATRIX_HOMESERVER,
    AgentMatrixUser,
    create_mention_content_from_text,
    ensure_all_agent_users,
    fetch_thread_history,
    get_room_aliases,
    login_agent_user,
)
from .response_tracker import ResponseTracker
from .room_invites import room_invite_manager
from .routing import suggest_agent_for_message
from .thread_invites import thread_invite_manager
from .thread_utils import (
    get_agents_in_thread,
    get_available_agents_in_room,
    get_mentioned_agents,
    has_any_agent_mentions_in_thread,
)

logger = get_logger(__name__)


def is_sender_other_agent(sender: str, current_agent_user_id: str) -> bool:
    """Check if sender is another agent (not the current agent, not the user)."""
    if sender == current_agent_user_id:
        return False

    sender_username = sender.split(":")[0][1:] if sender.startswith("@") else ""
    # Check if it's a mindroom agent (not a regular user)
    return sender_username.startswith("mindroom_") and not sender_username.startswith("mindroom_user")


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
            self.client = await login_agent_user(MATRIX_HOMESERVER, self.agent_user)

            # Initialize response tracker
            self.response_tracker = ResponseTracker(self.agent_name, self.storage_path)

            # Register event callbacks
            logger.debug("Registering event callbacks", agent=f"{emoji(self.agent_name)} {self.agent_name}")
            self.client.add_event_callback(self._on_invite, nio.InviteEvent)
            self.client.add_event_callback(self._on_message, nio.RoomMessageText)
            logger.debug("Event callbacks registered", agent=f"{emoji(self.agent_name)} {self.agent_name}")

            self.running = True
            logger.info(
                "Started agent bot",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                display_name=self.agent_user.display_name,
                user_id=self.agent_user.user_id,
            )

            # Join configured rooms
            for room_id in self.rooms:
                try:
                    response = await self.client.join(room_id)
                    if isinstance(response, nio.JoinResponse):
                        logger.info("Joined room", agent=f"{emoji(self.agent_name)} {self.agent_name}", room_id=room_id)
                    else:
                        logger.warning(
                            "Could not join room",
                            agent=f"{emoji(self.agent_name)} {self.agent_name}",
                            room_id=room_id,
                            error=str(response),
                        )
                except Exception as e:
                    logger.error(
                        "Error joining room",
                        agent=f"{emoji(self.agent_name)} {self.agent_name}",
                        room_id=room_id,
                        error=str(e),
                    )

        except Exception as e:
            logger.error("Failed to start", agent=f"{emoji(self.agent_name)} {self.agent_name}", error=str(e))
            raise

    async def sync_forever(self) -> None:
        """Run the sync loop forever."""
        if not self.client:
            return

        logger.info("Starting sync_forever", agent=f"{emoji(self.agent_name)} {self.agent_name}")
        try:
            await self.client.sync_forever(timeout=30000, full_state=True)
        except Exception as e:
            logger.error("Error in sync_forever", agent=f"{emoji(self.agent_name)} {self.agent_name}", error=str(e))
            raise

    async def stop(self) -> None:
        """Stop the agent bot."""
        self.running = False
        if self.client:
            await self.client.close()
        logger.info("Stopped agent bot", agent=f"{emoji(self.agent_name)} {self.agent_name}")

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle room invitations."""
        logger.info(
            "Received invite to room",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            room_id=room.room_id,
            sender=event.sender,
        )
        if self.client:
            result = await self.client.join(room.room_id)
            if isinstance(result, nio.JoinResponse):
                logger.info("Joined room", agent=f"{emoji(self.agent_name)} {self.agent_name}", room_id=room.room_id)
            else:
                logger.error(
                    "Failed to join room",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    room_id=room.room_id,
                    error=str(result),
                )

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
            logger.debug("Ignoring own message", agent=f"{emoji(self.agent_name)} {self.agent_name}")
            return

        # Don't respond to other agent messages (avoid agent loops)
        if is_sender_other_agent(event.sender, self.agent_user.user_id):
            logger.debug(
                "Ignoring message from other agent",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                sender=event.sender,
            )
            return

        # Track activity for room invites (if I'm in this room due to invite)
        is_room_invite = await room_invite_manager.is_agent_invited_to_room(room.room_id, self.agent_name)
        if is_room_invite:
            await room_invite_manager.record_agent_activity(room.room_id, self.agent_name)
            logger.debug(
                "Recorded activity for room invite",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                room_id=room.room_id,
            )

        # Check if I should be in this room (native or invited)
        if room.room_id not in self.rooms and not is_room_invite:
            logger.debug(
                "Not in this room and not invited",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                room_id=room.room_id,
            )
            return

        # Check for commands (only general agent handles commands)
        if self.agent_name == "general":
            command = command_parser.parse(event.body)
            if command:
                await self._handle_command(room, event, command)
                return

        # Debug logging
        logger.debug(
            "Checking message",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            body=event.body,
            user_id=self.agent_user.user_id,
            display_name=self.agent_user.display_name,
        )

        # Extract mentions and thread info
        mentions = event.source.get("content", {}).get("m.mentions", {})
        mentioned_agents = get_mentioned_agents(mentions)
        am_i_mentioned = self.agent_name in mentioned_agents

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

        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
        thread_id = relates_to.get("event_id") if is_thread else None

        # Fetch thread history if in thread
        thread_history = []
        if thread_id:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        # Check if I'm invited to this thread (even if not in this room normally)
        is_invited_to_thread = False
        if thread_id:
            is_invited_to_thread = await thread_invite_manager.is_agent_invited_to_thread(thread_id, self.agent_name)
            if is_invited_to_thread:
                logger.debug(
                    "Agent is invited to this thread",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    thread_id=thread_id,
                )

        # Decision logic:
        # 1. If I'm mentioned → I respond (always)
        # 2. If I'm invited to thread → I can participate
        # 3. If any agent is mentioned in thread → Only mentioned agents respond
        # 4. If NO agents are mentioned in thread:
        #    - 0 agents have participated → Router picks who should start
        #    - 1 agent has participated → That agent continues
        #    - 2+ agents have participated → Nobody responds (users must mention who they want)
        # 5. Not in thread → Don't respond

        should_respond = False
        use_router = False

        if am_i_mentioned:
            # Always respond if explicitly mentioned
            should_respond = True
            logger.debug("Will respond: explicitly mentioned", agent=f"{emoji(self.agent_name)} {self.agent_name}")
        elif is_thread:
            # Check if I'm an invited agent who should participate
            if is_invited_to_thread:
                # I'm invited - check if I should respond
                if has_any_agent_mentions_in_thread(thread_history):
                    # Someone is mentioned - only respond if I'm mentioned
                    logger.debug(
                        "Invited but not responding: other agents mentioned",
                        agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    )
                else:
                    # No mentions - invited agents can participate
                    agents_in_thread = get_agents_in_thread(thread_history)
                    if len(agents_in_thread) == 1 and self.agent_name in agents_in_thread:
                        # I'm the only agent who has responded - continue
                        should_respond = True
                        logger.debug(
                            "Will respond: invited and only agent in thread",
                            agent=f"{emoji(self.agent_name)} {self.agent_name}",
                        )
                    elif not agents_in_thread:
                        # No agents yet but I'm invited - I can start
                        should_respond = True
                        logger.debug(
                            "Will respond: invited to thread with no agents yet",
                            agent=f"{emoji(self.agent_name)} {self.agent_name}",
                        )
            elif room.room_id in self.rooms:
                # Not invited but in the room - standard logic
                if has_any_agent_mentions_in_thread(thread_history):
                    # Someone is mentioned - only mentioned agents respond
                    logger.debug(
                        "Not responding: other agents mentioned in thread",
                        agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    )
                else:
                    # No mentions - check agent participation
                    agents_in_thread = get_agents_in_thread(thread_history)
                    if len(agents_in_thread) == 1 and self.agent_name in agents_in_thread:
                        # I'm the only agent in thread - continue responding
                        should_respond = True
                        logger.debug(
                            "Will respond: only agent in thread", agent=f"{emoji(self.agent_name)} {self.agent_name}"
                        )
                    elif not agents_in_thread:
                        # No agents yet - use router to pick first responder
                        use_router = True
                        logger.debug(
                            "Not responding: no agents yet, will use router",
                            agent=f"{emoji(self.agent_name)} {self.agent_name}",
                        )
                    else:
                        # Multiple agents - nobody responds
                        logger.debug(
                            "Not responding: multiple agents in thread, need explicit mention",
                            agent=f"{emoji(self.agent_name)} {self.agent_name}",
                            agents_in_thread=agents_in_thread,
                        )
        else:
            # Not in thread and not mentioned
            logger.debug(
                "Not responding: not in thread or mentioned", agent=f"{emoji(self.agent_name)} {self.agent_name}"
            )

        # Handle routing if needed
        if use_router:
            await self._handle_ai_routing(room, event, thread_history)
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

        logger.info(
            "WILL PROCESS message",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            sender=event.sender,
            body=event.body,
            event_id=event.event_id,
        )

        # For now, use the full message body as the prompt
        # The actual mention text might not be in the body with modern Matrix clients
        prompt = event.body.strip()

        if not prompt:
            return

        # Create session ID with thread awareness
        session_id = f"{room.room_id}:{thread_id}" if thread_id else room.room_id

        # Fetch thread history if we haven't already
        if thread_id and not thread_history:
            thread_history = await fetch_thread_history(self.client, room.room_id, thread_id)

        # Generate response with room context
        response_text = await ai_response(
            agent_name=self.agent_name,
            prompt=prompt,
            session_id=session_id,
            storage_path=self.storage_path,
            thread_history=thread_history,
            room_id=room.room_id,
        )

        # Prepare and send response with proper mention parsing
        # Extract domain from agent's user_id
        sender_domain = self.agent_user.user_id.split(":")[1] if ":" in self.agent_user.user_id else "localhost"

        # Parse response for any agent mentions
        content = create_mention_content_from_text(
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
            reply_to_event_id=event.event_id if thread_id else None,
        )

        logger.debug(
            "Sending response",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            room_id=room.room_id,
            message_type="m.room.message",
            content=content,
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
                logger.info(
                    "Sent response to room",
                    agent=f"{emoji(self.agent_name)} {self.agent_name}",
                    room_id=room.room_id,
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
        """Handle AI routing for multi-agent threads."""
        # Only let one agent do the routing to avoid duplicates
        # All agents receive the message, but we need only one to handle routing
        # We use the first agent alphabetically as a deterministic choice
        # Example: If room has [calculator, general, shell], only calculator routes
        available_agents = get_available_agents_in_room(room)
        if not available_agents or available_agents[0] != self.agent_name:
            # I'm not the first agent alphabetically, so I should not route
            return  # Let the first agent handle routing

        logger.info(
            "Handling AI routing",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            body_preview=event.body[:50],
            event_id=event.event_id,
        )

        # Get thread info if available
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        thread_event_id = relates_to.get("event_id") if relates_to else None

        # Get AI suggestion (including invited agents)
        suggested_agent = await suggest_agent_for_message(event.body, available_agents, thread_history, thread_event_id)
        if not suggested_agent:
            return

        # Send mention to suggested agent
        response_text = "could you help with this?"

        # Use universal mention parser
        sender_domain = self.agent_user.user_id.split(":")[1] if ":" in self.agent_user.user_id else "localhost"
        full_message = f"@{suggested_agent} {response_text}"

        content = create_mention_content_from_text(
            full_message,
            sender_domain=sender_domain,
            thread_event_id=thread_event_id,
            reply_to_event_id=event.event_id,
        )

        if self.client:
            await self.client.room_send(room_id=room.room_id, message_type="m.room.message", content=content)
            logger.info(
                "Routed to agent",
                agent=f"{emoji(self.agent_name)} {self.agent_name}",
                suggested_agent=suggested_agent,
                room_id=room.room_id,
            )

    async def _handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageText, command: Command) -> None:
        """Handle user commands."""
        logger.info(
            "Handling command",
            agent=f"{emoji(self.agent_name)} {self.agent_name}",
            command_type=command.type.value,
            args=command.args,
        )

        # Get thread info
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        is_thread = relates_to and relates_to.get("rel_type") == "m.thread"
        thread_id = relates_to.get("event_id") if is_thread else None

        response_text = ""

        if command.type == CommandType.INVITE:
            # Handle invite command
            agent_name = command.args["agent_name"]
            to_room = command.args.get("to_room", False)
            duration_hours = command.args.get("duration_hours")

            # Check if agent exists
            config = load_config()
            if agent_name not in config.agents:
                response_text = f"❌ Unknown agent: {agent_name}. Available agents: {', '.join(config.agents.keys())}"
            elif to_room:
                # Room invite
                # Add room invitation
                await room_invite_manager.add_invite(
                    room_id=room.room_id,
                    agent_name=agent_name,
                    invited_by=event.sender,
                    inactivity_timeout_hours=duration_hours or 24,  # Default 24h
                )

                # Get the agent's user ID and invite to Matrix room
                agent_user_id = f"@mindroom_{agent_name}:{self.agent_user.user_id.split(':')[1]}"
                try:
                    if self.client:
                        result = await self.client.room_invite(room.room_id, agent_user_id)
                        if isinstance(result, nio.RoomInviteResponse):
                            timeout_text = f"{duration_hours} hours" if duration_hours else "24 hours"
                            response_text = f"✅ Invited @{agent_name} to this room. They will be removed after {timeout_text} of inactivity."
                        else:
                            response_text = f"❌ Failed to invite @{agent_name}: {result}"
                            # Remove the invite record if Matrix invite failed
                            await room_invite_manager.remove_invite(room.room_id, agent_name)
                except Exception as e:
                    response_text = f"❌ Error inviting @{agent_name}: {str(e)}"
                    # Remove the invite record if Matrix invite failed
                    await room_invite_manager.remove_invite(room.room_id, agent_name)
            else:
                # Thread invite
                if not thread_id:
                    response_text = "❌ Thread invites can only be used in a thread. Use '/invite <agent> to room' for room invites."
                else:
                    # Add the invitation
                    await thread_invite_manager.add_invite(
                        thread_id=thread_id,
                        room_id=room.room_id,
                        agent_name=agent_name,
                        invited_by=event.sender,
                        duration_hours=duration_hours,
                    )

                    duration_text = f" for {duration_hours} hours" if duration_hours else " until thread ends"
                    response_text = f"✅ Invited @{agent_name} to this thread{duration_text}. They can now participate even if not in this room."

                    # Mention the agent so they know they're invited
                    response_text += f"\n\n@{agent_name}, you've been invited to help in this thread!"

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
            response_parts = []

            # Get room invites
            room_invites = await room_invite_manager.get_room_invites(room.room_id)
            if room_invites:
                room_list = "\n".join([f"- @{agent} (room invite)" for agent in room_invites])
                response_parts.append(f"**Room invites:**\n{room_list}")

            # Get thread invites if in a thread
            if thread_id:
                thread_invites = await thread_invite_manager.get_thread_agents(thread_id)
                if thread_invites:
                    thread_list = "\n".join([f"- @{agent} (thread invite)" for agent in thread_invites])
                    response_parts.append(f"**Thread invites:**\n{thread_list}")

            if response_parts:
                response_text = "\n\n".join(response_parts)
            else:
                response_text = "No agents are currently invited to this room or thread."

        elif command.type == CommandType.HELP:
            # Handle help command
            topic = command.args.get("topic")
            response_text = get_command_help(topic)

        # Send response
        if response_text:
            sender_domain = self.agent_user.user_id.split(":")[1] if ":" in self.agent_user.user_id else "localhost"
            content = create_mention_content_from_text(
                response_text,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
                reply_to_event_id=event.event_id if thread_id else None,
            )

            if self.client:
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
        """Periodically clean up expired invitations and check room invite activity."""
        logger.info("Starting periodic cleanup task")

        while self.running:
            try:
                # Wait for 1 minute between checks
                await asyncio.sleep(60)  # 1 minute

                # Clean up expired thread invitations
                thread_removed = await thread_invite_manager.cleanup_expired()
                if thread_removed > 0:
                    logger.info(f"Cleaned up {thread_removed} expired thread invitations")

                # Check room invitations for inactivity
                client = None
                if "general" in self.agent_bots and self.agent_bots["general"].client:
                    client = self.agent_bots["general"].client

                # Get all room invites
                inactive_count = 0
                async with room_invite_manager._lock:
                    for room_id, room_invites in list(room_invite_manager._room_invites.items()):
                        for agent_name, invite in list(room_invites.items()):
                            if invite.is_inactive():
                                # Check if agent has active thread invites in this room
                                thread_invites = await thread_invite_manager.get_agent_threads(room_id, agent_name)

                                if thread_invites:
                                    # Agent is active in threads, update their room activity
                                    invite.update_activity()
                                    logger.debug(
                                        "Updated room activity due to thread participation",
                                        agent=agent_name,
                                        room_id=room_id,
                                        active_threads=len(thread_invites),
                                    )
                                else:
                                    # No thread activity, mark for removal
                                    inactive_count += 1

                # Now run the standard cleanup which will kick inactive agents
                if inactive_count > 0:
                    room_removed = await room_invite_manager.cleanup_expired(client)
                    if room_removed > 0:
                        logger.info(f"Kicked {room_removed} inactive agents from rooms")

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
