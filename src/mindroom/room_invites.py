"""Room-level agent invitation management with activity tracking."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import nio

from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class RoomInvite:
    """Represents a temporary agent invitation to a room."""

    room_id: str
    agent_name: str
    invited_by: str
    invited_at: datetime
    last_activity: datetime
    inactivity_timeout_hours: int = 24  # Default 24 hours

    def is_inactive(self) -> bool:
        """Check if the invitation should expire due to inactivity."""
        timeout = timedelta(hours=self.inactivity_timeout_hours)
        return datetime.now() - self.last_activity > timeout

    def update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.last_activity = datetime.now()


@dataclass
class AgentActivity:
    """Tracks agent activity in rooms."""

    agent_name: str
    room_activities: dict[str, datetime] = field(default_factory=dict)

    def record_activity(self, room_id: str) -> None:
        """Record activity for the agent in a specific room."""
        self.room_activities[room_id] = datetime.now()

    def get_last_activity(self, room_id: str) -> datetime | None:
        """Get the last activity time for a room."""
        return self.room_activities.get(room_id)


class RoomInviteManager:
    """Manages temporary agent invitations to rooms with activity tracking."""

    def __init__(self):
        # Map of room_id -> agent_name -> RoomInvite
        self._room_invites: dict[str, dict[str, RoomInvite]] = {}
        # Map of agent_name -> AgentActivity
        self._agent_activities: dict[str, AgentActivity] = {}
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def add_room_invite(
        self,
        room_id: str,
        agent_name: str,
        invited_by: str,
        inactivity_timeout_hours: int = 24,
    ) -> RoomInvite:
        """Add a temporary agent invitation to a room.

        Args:
            room_id: The room ID to invite the agent to
            agent_name: Name of the agent being invited
            invited_by: User ID who invited the agent
            inactivity_timeout_hours: Hours of inactivity before auto-kick (default 24)

        Returns:
            The created RoomInvite
        """
        async with self._lock:
            # Create the invitation
            now = datetime.now()
            invite = RoomInvite(
                room_id=room_id,
                agent_name=agent_name,
                invited_by=invited_by,
                invited_at=now,
                last_activity=now,
                inactivity_timeout_hours=inactivity_timeout_hours,
            )

            # Add to room invites
            if room_id not in self._room_invites:
                self._room_invites[room_id] = {}
            self._room_invites[room_id][agent_name] = invite

            # Initialize agent activity tracking
            if agent_name not in self._agent_activities:
                self._agent_activities[agent_name] = AgentActivity(agent_name)
            self._agent_activities[agent_name].record_activity(room_id)

            logger.info(
                "Added room invitation",
                room_id=room_id,
                agent=agent_name,
                invited_by=invited_by,
                timeout_hours=inactivity_timeout_hours,
            )

            return invite

    async def record_agent_activity(
        self,
        room_id: str,
        agent_name: str,
    ) -> None:
        """Record that an agent was active in a room.

        Args:
            room_id: The room ID where activity occurred
            agent_name: Name of the active agent
        """
        async with self._lock:
            # Update room invite activity if exists
            if room_id in self._room_invites and agent_name in self._room_invites[room_id]:
                self._room_invites[room_id][agent_name].update_activity()

            # Update agent activity tracking
            if agent_name not in self._agent_activities:
                self._agent_activities[agent_name] = AgentActivity(agent_name)
            self._agent_activities[agent_name].record_activity(room_id)

            logger.debug(
                "Recorded agent activity",
                room_id=room_id,
                agent=agent_name,
            )

    async def is_agent_invited_to_room(
        self,
        room_id: str,
        agent_name: str,
    ) -> bool:
        """Check if an agent is invited to a room.

        Args:
            room_id: The room ID
            agent_name: Name of the agent

        Returns:
            True if agent is invited and not inactive
        """
        async with self._lock:
            if room_id not in self._room_invites:
                return False

            if agent_name not in self._room_invites[room_id]:
                return False

            invite = self._room_invites[room_id][agent_name]
            return not invite.is_inactive()

    async def get_room_invites(self, room_id: str) -> list[str]:
        """Get list of agents invited to a room.

        Args:
            room_id: The room ID

        Returns:
            List of agent names invited to the room (excluding inactive)
        """
        async with self._lock:
            if room_id not in self._room_invites:
                return []

            active_agents = []
            for agent_name, invite in self._room_invites[room_id].items():
                if not invite.is_inactive():
                    active_agents.append(agent_name)

            return active_agents

    async def remove_room_invite(
        self,
        room_id: str,
        agent_name: str,
    ) -> bool:
        """Remove an agent invitation from a room.

        Args:
            room_id: The room ID
            agent_name: Name of the agent

        Returns:
            True if invitation was found and removed
        """
        async with self._lock:
            if room_id not in self._room_invites:
                return False

            if agent_name in self._room_invites[room_id]:
                del self._room_invites[room_id][agent_name]

                # Clean up empty room
                if not self._room_invites[room_id]:
                    del self._room_invites[room_id]

                logger.info(
                    "Removed room invitation",
                    room_id=room_id,
                    agent=agent_name,
                )
                return True

            return False

    async def get_inactive_invites(self) -> list[tuple[str, str]]:
        """Get all inactive invitations that should be cleaned up.

        Returns:
            List of (room_id, agent_name) tuples for inactive invites
        """
        async with self._lock:
            inactive = []

            for room_id, invites in self._room_invites.items():
                for agent_name, invite in invites.items():
                    if invite.is_inactive():
                        inactive.append((room_id, agent_name))

            return inactive

    async def cleanup_inactive_invites(self, client: nio.AsyncClient | None = None) -> int:
        """Remove all inactive invitations and optionally kick agents from rooms.

        Args:
            client: Optional Matrix client to perform actual room kicks

        Returns:
            Number of invitations removed
        """
        # Import here to avoid circular imports
        from mindroom.thread_activity import alien_activity_tracker

        # Get inactive invites outside the lock to avoid holding it during Matrix operations
        inactive_invites = await self.get_inactive_invites()

        if not inactive_invites:
            return 0

        removed_count = 0

        for room_id, agent_name in inactive_invites:
            # Check global alien agent activity before kicking
            agent_activity = await alien_activity_tracker.get_agent_activity(agent_name, room_id)

            # If agent has been active in any thread in this room in the last 24 hours, don't kick
            if agent_activity:
                cutoff = datetime.now() - timedelta(hours=24)
                if agent_activity.last_active > cutoff:
                    logger.info(
                        "Skipping kick for agent due to recent thread activity",
                        room_id=room_id,
                        agent=agent_name,
                        last_active=agent_activity.last_active.isoformat(),
                        active_threads=agent_activity.active_threads,
                    )
                    # Update the room invite activity to prevent repeated checks
                    await self.record_agent_activity(room_id, agent_name)
                    continue

            # Remove the invitation
            if await self.remove_room_invite(room_id, agent_name):
                removed_count += 1

                # If we have a client, actually kick the agent from the room
                if client:
                    try:
                        # Get the agent's user ID
                        agent_user_id = f"@mindroom_{agent_name}:localhost"  # Adjust domain as needed

                        # Kick from room
                        result = await client.room_kick(
                            room_id,
                            agent_user_id,
                            reason="Inactive for 24 hours - automatic removal",
                        )

                        if isinstance(result, nio.RoomKickResponse):
                            logger.info(
                                "Kicked inactive agent from room",
                                room_id=room_id,
                                agent=agent_name,
                            )
                            # Clean up alien activity tracking
                            await alien_activity_tracker.cleanup_agent_activity(agent_name, room_id)
                        else:
                            logger.error(
                                "Failed to kick agent from room",
                                room_id=room_id,
                                agent=agent_name,
                                error=str(result),
                            )
                    except Exception as e:
                        logger.error(
                            "Error kicking agent from room",
                            room_id=room_id,
                            agent=agent_name,
                            error=str(e),
                        )

        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} inactive room invitations")

        return removed_count


# Global room invite manager instance
room_invite_manager = RoomInviteManager()
