"""Thread-specific agent invitation management using Matrix state events."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import nio

from .logging_config import get_logger

logger = get_logger(__name__)

# Custom event type for thread invitations
THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"


@dataclass
class ThreadInvite:
    """Represents a temporary agent invitation to a thread."""

    agent_name: str
    invited_by: str
    invited_at: datetime
    thread_id: str
    room_id: str
    expires_at: datetime | None = None

    def is_expired(self) -> bool:
        """Check if the invitation has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def to_dict(self) -> dict:
        """Convert to dictionary for Matrix state event."""
        return {
            "agent_name": self.agent_name,
            "invited_by": self.invited_by,
            "invited_at": self.invited_at.isoformat(),
            "thread_id": self.thread_id,
            "room_id": self.room_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ThreadInvite":
        """Create from dictionary from Matrix state event."""
        return cls(
            agent_name=data["agent_name"],
            invited_by=data["invited_by"],
            invited_at=datetime.fromisoformat(data["invited_at"]),
            thread_id=data["thread_id"],
            room_id=data["room_id"],
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
        )


class ThreadInviteManager:
    """Manages temporary agent invitations for specific threads using Matrix state events."""

    def __init__(self, client: nio.AsyncClient):
        self._lock = asyncio.Lock()
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
        duration_hours: int | None = None,
    ) -> ThreadInvite:
        """Add a temporary agent invitation to a thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists
            agent_name: Name of the agent being invited
            invited_by: User ID who invited the agent
            duration_hours: Optional duration in hours (None = until thread ends)

        Returns:
            The created ThreadInvite
        """
        async with self._lock:
            expires_at = datetime.now() + timedelta(hours=duration_hours) if duration_hours else None

            invite = ThreadInvite(
                thread_id=thread_id,
                room_id=room_id,
                agent_name=agent_name,
                invited_by=invited_by,
                invited_at=datetime.now(),
                expires_at=expires_at,
            )

            # Store as Matrix state event
            state_key = self._get_state_key(thread_id, agent_name)
            response = await self.client.room_put_state(
                room_id=room_id,
                event_type=THREAD_INVITE_EVENT_TYPE,
                content=invite.to_dict(),
                state_key=state_key,
            )

            if isinstance(response, nio.RoomPutStateResponse):
                logger.info(
                    "Added thread invitation as state event",
                    thread_id=thread_id,
                    room_id=room_id,
                    agent=agent_name,
                    invited_by=invited_by,
                    expires_at=expires_at,
                    event_id=response.event_id,
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

            return invite

    async def get_thread_agents(self, thread_id: str, room_id: str) -> list[str]:
        """Get list of agents invited to a specific thread.

        Args:
            thread_id: The thread event ID
            room_id: The room ID where the thread exists

        Returns:
            List of agent names invited to the thread
        """
        async with self._lock:
            # Get room state to find all thread invitations
            response = await self.client.room_get_state(room_id)

            if not isinstance(response, nio.RoomGetStateResponse):
                logger.error("Failed to get room state", room_id=room_id, error=str(response))
                return []

            active_agents = []
            for event in response.events:
                # Check if this is a thread invite event
                if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                    state_key = event.get("state_key", "")
                    # Check if this state key is for our thread
                    if state_key.startswith(f"{thread_id}:"):
                        content = event.get("content", {})
                        if content:
                            invite = ThreadInvite.from_dict(content)
                            if not invite.is_expired():
                                active_agents.append(invite.agent_name)

            return active_agents

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
            True if agent is invited and invitation is active
        """
        async with self._lock:
            # Get specific state event for this thread/agent combination
            state_key = self._get_state_key(thread_id, agent_name)
            response = await self.client.room_get_state_event(
                room_id=room_id,
                event_type=THREAD_INVITE_EVENT_TYPE,
                state_key=state_key,
            )

            if isinstance(response, nio.RoomGetStateEventResponse):
                content = response.content
                if content:
                    invite = ThreadInvite.from_dict(content)
                    return not invite.is_expired()

            return False

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
        async with self._lock:
            # Get room state to find all thread invitations for this agent
            response = await self.client.room_get_state(room_id)

            if not isinstance(response, nio.RoomGetStateResponse):
                logger.error("Failed to get room state", room_id=room_id, error=str(response))
                return []

            active_threads = []
            for event in response.events:
                if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                    state_key = event.get("state_key", "")
                    # Check if this state key includes our agent
                    if state_key.endswith(f":{agent_name}"):
                        content = event.get("content", {})
                        if content:
                            invite = ThreadInvite.from_dict(content)
                            if not invite.is_expired():
                                active_threads.append(invite.thread_id)

            return active_threads

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
        async with self._lock:
            # Check if invitation exists
            state_key = self._get_state_key(thread_id, agent_name)
            check_response = await self.client.room_get_state_event(
                room_id=room_id,
                event_type=THREAD_INVITE_EVENT_TYPE,
                state_key=state_key,
            )

            if not isinstance(check_response, nio.RoomGetStateEventResponse):
                return False

            # Remove by sending empty content to the same state key
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

    async def cleanup_expired(self, room_id: str) -> int:
        """Remove all expired invitations in a room.

        Args:
            room_id: The room ID to clean up

        Returns:
            Number of invitations removed
        """
        async with self._lock:
            # Get room state to find all thread invitations
            response = await self.client.room_get_state(room_id)

            if not isinstance(response, nio.RoomGetStateResponse):
                logger.error("Failed to get room state for cleanup", room_id=room_id, error=str(response))
                return 0

            removed_count = 0
            expired_events = []

            for event in response.events:
                if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                    content = event.get("content", {})
                    if content:  # Only process non-empty events
                        invite = ThreadInvite.from_dict(content)
                        if invite.is_expired():
                            state_key = event.get("state_key", "")
                            expired_events.append((invite.thread_id, invite.agent_name, state_key))

            # Remove expired invitations
            for thread_id, agent_name, state_key in expired_events:
                response = await self.client.room_put_state(
                    room_id=room_id,
                    event_type=THREAD_INVITE_EVENT_TYPE,
                    content={},  # Empty content removes the state event
                    state_key=state_key,
                )

                if isinstance(response, nio.RoomPutStateResponse):
                    removed_count += 1
                    logger.info(
                        "Removed expired thread invitation",
                        thread_id=thread_id,
                        agent=agent_name,
                    )
                else:
                    logger.error(
                        "Failed to remove expired invitation",
                        thread_id=thread_id,
                        agent=agent_name,
                        error=str(response),
                    )

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} expired thread invitations in room {room_id}")

            return removed_count
