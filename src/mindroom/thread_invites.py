"""Thread-specific agent invitation management."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from .logging_config import get_logger

logger = get_logger(__name__)


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


class ThreadInviteManager:
    """Manages temporary agent invitations for specific threads."""

    def __init__(self):
        self._lock = asyncio.Lock()
        # Map of thread_id -> list of ThreadInvite
        self._invites: dict[str, list[ThreadInvite]] = {}
        # Map of (room_id, agent_name) -> set of thread_ids
        self._agent_threads: dict[tuple[str, str], set[str]] = {}

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

            # Add to thread invites
            if thread_id not in self._invites:
                self._invites[thread_id] = []
            self._invites[thread_id].append(invite)

            # Add to agent threads index
            key = (room_id, agent_name)
            if key not in self._agent_threads:
                self._agent_threads[key] = set()
            self._agent_threads[key].add(thread_id)

            logger.info(
                "Added thread invitation",
                thread_id=thread_id,
                room_id=room_id,
                agent=agent_name,
                invited_by=invited_by,
                expires_at=expires_at,
            )

            return invite

    async def get_thread_agents(self, thread_id: str) -> list[str]:
        """Get list of agents invited to a specific thread.

        Args:
            thread_id: The thread event ID

        Returns:
            List of agent names invited to the thread
        """
        async with self._lock:
            invites = self._invites.get(thread_id, [])
            # Filter out expired invites
            active_invites = [inv for inv in invites if not inv.is_expired()]
            return [inv.agent_name for inv in active_invites]

    async def is_agent_invited_to_thread(
        self,
        thread_id: str,
        agent_name: str,
    ) -> bool:
        """Check if an agent is invited to a specific thread.

        Args:
            thread_id: The thread event ID
            agent_name: Name of the agent

        Returns:
            True if agent is invited and invitation is active
        """
        agents = await self.get_thread_agents(thread_id)
        return agent_name in agents

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
            key = (room_id, agent_name)
            thread_ids = self._agent_threads.get(key, set())

            # Filter out threads with expired invites
            active_threads = []
            for thread_id in thread_ids:
                invites = self._invites.get(thread_id, [])
                if any(inv.agent_name == agent_name and not inv.is_expired() for inv in invites):
                    active_threads.append(thread_id)

            return active_threads

    async def remove_invite(
        self,
        thread_id: str,
        agent_name: str,
    ) -> bool:
        """Remove an agent invitation from a thread.

        Args:
            thread_id: The thread event ID
            agent_name: Name of the agent

        Returns:
            True if invitation was found and removed
        """
        async with self._lock:
            if thread_id not in self._invites:
                return False

            invites = self._invites[thread_id]
            original_count = len(invites)

            # Remove matching invites
            self._invites[thread_id] = [inv for inv in invites if inv.agent_name != agent_name]

            # Clean up empty lists
            if not self._invites[thread_id]:
                del self._invites[thread_id]

            # Update agent threads index
            for inv in invites:
                if inv.agent_name == agent_name:
                    key = (inv.room_id, agent_name)
                    if key in self._agent_threads:
                        self._agent_threads[key].discard(thread_id)
                        if not self._agent_threads[key]:
                            del self._agent_threads[key]

            removed = len(self._invites.get(thread_id, [])) != original_count
            if removed:
                logger.info(
                    "Removed thread invitation",
                    thread_id=thread_id,
                    agent=agent_name,
                )

            return removed

    async def cleanup_expired(self) -> int:
        """Remove all expired invitations.

        Returns:
            Number of invitations removed
        """
        async with self._lock:
            removed_count = 0

            # Collect threads to clean up
            threads_to_clean = []
            for thread_id, invites in self._invites.items():
                expired_agents = [inv.agent_name for inv in invites if inv.is_expired()]
                if expired_agents:
                    threads_to_clean.append((thread_id, expired_agents))

            # Clean up expired invites
            for thread_id, expired_agents in threads_to_clean:
                for agent_name in expired_agents:
                    # Remove from thread invites
                    self._invites[thread_id] = [
                        inv
                        for inv in self._invites[thread_id]
                        if not (inv.agent_name == agent_name and inv.is_expired())
                    ]
                    removed_count += 1

                    # Clean up agent threads index for expired invites
                    for inv in invites:
                        if inv.agent_name == agent_name and inv.is_expired():
                            key = (inv.room_id, agent_name)
                            if key in self._agent_threads:
                                self._agent_threads[key].discard(thread_id)
                                if not self._agent_threads[key]:
                                    del self._agent_threads[key]

                # Clean up empty lists
                if not self._invites[thread_id]:
                    del self._invites[thread_id]

            # Clean up empty agent thread sets
            empty_keys = [key for key, threads in self._agent_threads.items() if not threads]
            for key in empty_keys:
                del self._agent_threads[key]

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} expired thread invitations")

            return removed_count


# Global thread invite manager instance
thread_invite_manager = ThreadInviteManager()
