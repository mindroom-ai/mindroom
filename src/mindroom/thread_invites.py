"""Thread-specific agent invitation management using Matrix state events."""

import nio

from .logging_config import get_logger

logger = get_logger(__name__)

# Custom event type for thread invitations
THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"


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
        content = {"invited_by": invited_by}

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
