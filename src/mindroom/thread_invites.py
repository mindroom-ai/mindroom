"""Thread-specific agent invitation management using Matrix state events."""

from datetime import datetime, timedelta

import nio

from .logging_config import get_logger

logger = get_logger(__name__)

# Custom event type for thread invitations
THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"

# Default timeout for thread invitations (hours)
DEFAULT_TIMEOUT_HOURS = 24


class ThreadInviteManager:
    """Manages agent invitations for specific threads using Matrix state events."""

    def __init__(self, client: nio.AsyncClient):
        self.client = client

    def _get_state_key(self, thread_id: str, agent_name: str) -> str:
        """Generate state key for thread invitation."""
        return f"{thread_id}:{agent_name}"

    async def add_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
        invited_by: str,
    ) -> None:
        """Add an agent invitation to a thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists
            agent_name: Name of the agent being invited
            invited_by: User ID who invited the agent
        """
        state_key = self._get_state_key(thread_id, agent_name)

        content = {
            "invited_by": invited_by,
            "invited_at": datetime.now().isoformat(),
        }

        response = await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content=content,
            state_key=state_key,
        )

        if isinstance(response, nio.RoomPutStateResponse):
            logger.info(
                "Added thread invitation",
                thread_id=thread_id,
                room_id=room_id,
                agent=agent_name,
                invited_by=invited_by,
            )
        else:
            logger.error(
                "Failed to add thread invitation",
                thread_id=thread_id,
                agent=agent_name,
                error=str(response),
            )
            msg = f"Failed to add thread invitation: {response}"
            raise RuntimeError(msg)

    async def get_thread_agents(self, thread_id: str, room_id: str) -> list[str]:
        """Get list of agents invited to a specific thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists

        Returns:
            List of agent names invited to the thread
        """
        response = await self.client.room_get_state(room_id)

        if not isinstance(response, nio.RoomGetStateResponse):
            logger.error("Failed to get room state", room_id=room_id, error=str(response))
            return []

        agents = []
        for event in response.events:
            if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                state_key = event.get("state_key", "")
                # Check if this state key is for our thread
                if state_key.startswith(f"{thread_id}:"):
                    # Extract agent name from state key
                    agent_name = state_key.split(":", 1)[1]
                    agents.append(agent_name)

        return agents

    async def is_agent_invited_to_thread(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        """Check if an agent is invited to a specific thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists
            agent_name: Name of the agent

        Returns:
            True if agent is invited
        """
        state_key = self._get_state_key(thread_id, agent_name)
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key=state_key,
        )

        return isinstance(response, nio.RoomGetStateEventResponse)

    async def get_agent_threads(
        self,
        room_id: str,
        agent_name: str,
    ) -> list[str]:
        """Get list of threads an agent is invited to in a room.

        Args:
            room_id: The room ID
            agent_name: Name of the agent

        Returns:
            List of thread IDs the agent is invited to
        """
        response = await self.client.room_get_state(room_id)

        if not isinstance(response, nio.RoomGetStateResponse):
            logger.error("Failed to get room state", room_id=room_id, error=str(response))
            return []

        threads = []
        for event in response.events:
            if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                state_key = event.get("state_key", "")
                # Check if this state key includes our agent
                if state_key.endswith(f":{agent_name}"):
                    # Extract thread ID from state key
                    thread_id = state_key.rsplit(":", 1)[0]
                    threads.append(thread_id)

        return threads

    async def remove_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        """Remove an agent invitation from a thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists
            agent_name: Name of the agent

        Returns:
            True if invitation was found and removed
        """
        # Check if invitation exists
        if not await self.is_agent_invited_to_thread(thread_id, room_id, agent_name):
            return False

        # Remove by sending empty content to the same state key
        state_key = self._get_state_key(thread_id, agent_name)
        response = await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={},  # Empty content removes the state event
            state_key=state_key,
        )

        if isinstance(response, nio.RoomPutStateResponse):
            logger.info(
                "Removed thread invitation",
                thread_id=thread_id,
                room_id=room_id,
                agent=agent_name,
            )
            return True
        else:
            logger.error(
                "Failed to remove thread invitation",
                thread_id=thread_id,
                agent=agent_name,
                error=str(response),
            )
            return False


    async def cleanup_inactive_agents(self, room_id: str, timeout_hours: int = DEFAULT_TIMEOUT_HOURS) -> int:
        """Remove agents who haven't responded in the room for timeout_hours.

        Args:
            room_id: The room ID to clean up
            timeout_hours: Hours of inactivity before removing agent

        Returns:
            Number of agents removed from the room
        """
        # Get all thread invitations
        state_response = await self.client.room_get_state(room_id)
        if not isinstance(state_response, nio.RoomGetStateResponse):
            logger.error("Failed to get room state for cleanup", room_id=room_id, error=str(state_response))
            return 0

        # Find all invited agents
        invited_agents = []
        for event in state_response.events:
            if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                state_key = event.get("state_key", "")
                if ":" in state_key:
                    agent_name = state_key.split(":", 1)[1]
                    content = event.get("content", {})
                    invited_at_str = content.get("invited_at")
                    if invited_at_str:
                        try:
                            invited_at = datetime.fromisoformat(invited_at_str)
                            invited_agents.append((state_key, agent_name, invited_at))
                        except (ValueError, TypeError):
                            pass

        if not invited_agents:
            return 0

        # Get room messages to find last activity
        messages_response = await self.client.room_messages(
            room_id=room_id,
            start="",  # Get latest messages
            limit=1000,  # Get enough messages to find agent activity
        )
        
        if not isinstance(messages_response, nio.RoomMessagesResponse):
            logger.error("Failed to get room messages for cleanup", room_id=room_id)
            return 0

        # Build a map of agent -> last message timestamp
        agent_last_activity = {}
        for event in messages_response.chunk:
            if isinstance(event, nio.RoomMessageText):
                sender = event.sender
                # Extract agent name from sender ID
                if sender.startswith("@") and ":mindroom.space" in sender:
                    agent_name = sender.split("@")[1].split(":")[0]
                    # Track the most recent message (messages are in reverse chronological order)
                    if agent_name not in agent_last_activity:
                        # Convert server timestamp to datetime
                        timestamp = datetime.fromtimestamp(event.server_timestamp / 1000)
                        agent_last_activity[agent_name] = timestamp

        # Check each invited agent for inactivity
        removed_count = 0
        now = datetime.now()
        timeout_threshold = timedelta(hours=timeout_hours)

        for state_key, agent_name, invited_at in invited_agents:
            should_remove = False
            
            if agent_name in agent_last_activity:
                # Check if their last message was too long ago
                last_activity = agent_last_activity[agent_name]
                if now - last_activity > timeout_threshold:
                    should_remove = True
                    reason = f"inactive for {timeout_hours} hours"
            else:
                # Agent has never sent a message, check invitation time
                if now - invited_at > timeout_threshold:
                    should_remove = True
                    reason = "never responded"
            
            if should_remove:
                # Construct agent user ID
                agent_user_id = f"@{agent_name}:mindroom.space"

                # Kick the agent from the room
                kick_response = await self.client.room_kick(
                    room_id=room_id,
                    user_id=agent_user_id,
                    reason=f"Thread invitation expired: {reason}",
                )

                if isinstance(kick_response, nio.RoomKickResponse):
                    logger.info(
                        "Removed inactive agent from room",
                        agent=agent_name,
                        room_id=room_id,
                        reason=reason,
                    )

                    # Remove the invitation state event
                    remove_response = await self.client.room_put_state(
                        room_id=room_id,
                        event_type=THREAD_INVITE_EVENT_TYPE,
                        content={},  # Empty content removes the state event
                        state_key=state_key,
                    )

                    if isinstance(remove_response, nio.RoomPutStateResponse):
                        removed_count += 1
                    else:
                        logger.error(
                            "Failed to remove invitation after kicking agent",
                            agent=agent_name,
                            error=str(remove_response),
                        )
                else:
                    logger.error(
                        "Failed to kick inactive agent from room",
                        agent=agent_name,
                        room_id=room_id,
                        error=str(kick_response),
                    )

        if removed_count > 0:
            logger.info(f"Removed {removed_count} inactive agents from room {room_id}")

        return removed_count
