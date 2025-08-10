"""Thread-specific agent invitation management using Matrix state events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import nio

from .logging_config import get_logger
from .matrix.identity import MatrixID, ThreadStateKey

logger = get_logger(__name__)

THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"
AGENT_ACTIVITY_EVENT_TYPE = "com.mindroom.agent.activity"
DEFAULT_TIMEOUT_HOURS = 24


class ThreadInviteManager:
    """Manages thread-specific agent invitations using Matrix state events."""

    def __init__(self, client: nio.AsyncClient) -> None:
        self.client = client

    def _get_state_key(self, thread_id: str, agent_name: str) -> str:
        return ThreadStateKey(thread_id, agent_name).key

    async def add_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
        invited_by: str,
    ) -> None:
        now = datetime.now(tz=UTC).isoformat()
        await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={
                "invited_by": invited_by,
                "invited_at": now,
            },
            state_key=self._get_state_key(thread_id, agent_name),
        )
        # Also initialize agent activity tracking
        await self.update_agent_activity(room_id, agent_name)

    async def get_thread_agents(self, thread_id: str, room_id: str) -> list[str]:
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            return []

        agents = []
        for event in response.events:
            if event["type"] == THREAD_INVITE_EVENT_TYPE:
                state_key = event["state_key"]
                if state_key.startswith(f"{thread_id}:"):
                    key = ThreadStateKey.parse(state_key)
                    agents.append(key.agent_name)
        return agents

    async def is_agent_invited_to_thread(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key=self._get_state_key(thread_id, agent_name),
        )
        return isinstance(response, nio.RoomGetStateEventResponse)

    async def get_agent_threads(self, room_id: str, agent_name: str) -> list[str]:
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            return []

        threads = []
        for event in response.events:
            if event["type"] == THREAD_INVITE_EVENT_TYPE:
                state_key = event["state_key"]
                if state_key.endswith(f":{agent_name}"):
                    key = ThreadStateKey.parse(state_key)
                    threads.append(key.thread_id)
        return threads

    async def remove_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        if not await self.is_agent_invited_to_thread(thread_id, room_id, agent_name):
            return False

        response = await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={},
            state_key=self._get_state_key(thread_id, agent_name),
        )
        return isinstance(response, nio.RoomPutStateResponse)

    async def get_invite_state(self, thread_id: str, room_id: str, agent_name: str) -> dict | None:
        """Get the current invitation state for an agent in a thread."""
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key=self._get_state_key(thread_id, agent_name),
        )
        if isinstance(response, nio.RoomGetStateEventResponse):
            return response.content  # type: ignore[no-any-return]
        return None

    async def update_agent_activity(self, room_id: str, agent_name: str) -> None:
        """Update the last activity timestamp for an agent in a room."""
        await self.client.room_put_state(
            room_id=room_id,
            event_type=AGENT_ACTIVITY_EVENT_TYPE,
            content={
                "last_activity": datetime.now(tz=UTC).isoformat(),
            },
            state_key=agent_name,
        )

    async def get_agent_activity(self, room_id: str, agent_name: str) -> str | None:
        """Get the last activity timestamp for an agent in a room."""
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=AGENT_ACTIVITY_EVENT_TYPE,
            state_key=agent_name,
        )
        if isinstance(response, nio.RoomGetStateEventResponse):
            content = response.content
            return content.get("last_activity")  # type: ignore[no-any-return]
        return None

    async def cleanup_inactive_agents(self, room_id: str, timeout_hours: int = DEFAULT_TIMEOUT_HOURS) -> int:
        """Remove agents who haven't responded in the room for timeout_hours."""
        state_response = await self.client.room_get_state(room_id)
        if not isinstance(state_response, nio.RoomGetStateResponse):
            return 0

        # Get all invited agents (from thread invitations)
        invited_agents = []  # Use list to preserve order
        thread_invitations: dict[str, list[str]] = {}  # agent_name -> list of state_keys

        for event in state_response.events:
            if event["type"] == THREAD_INVITE_EVENT_TYPE:
                state_key = event["state_key"]
                key = ThreadStateKey.parse(state_key)
                if key.agent_name not in invited_agents:
                    invited_agents.append(key.agent_name)
                if key.agent_name not in thread_invitations:
                    thread_invitations[key.agent_name] = []
                thread_invitations[key.agent_name].append(state_key)

        if not invited_agents:
            return 0

        # Check activity for each invited agent
        now = datetime.now(tz=UTC)
        threshold = timedelta(hours=timeout_hours)
        agents_to_remove = []

        for agent_name in invited_agents:
            last_activity_str = await self.get_agent_activity(room_id, agent_name)
            if last_activity_str:
                try:
                    last_activity = datetime.fromisoformat(last_activity_str)
                    if now - last_activity > threshold:
                        agents_to_remove.append(agent_name)
                except (ValueError, TypeError):
                    # If we can't parse the activity, consider the agent for removal
                    agents_to_remove.append(agent_name)
            else:
                # No activity tracked, consider for removal
                agents_to_remove.append(agent_name)

        # Remove inactive agents
        removed_count = 0
        for agent_name in agents_to_remove:
            # Try to kick the agent from the room
            kick_response = await self.client.room_kick(
                room_id,
                MatrixID.from_agent(agent_name, MatrixID.DEFAULT_DOMAIN).full_id,
                f"Inactive for {timeout_hours} hours",
            )
            if isinstance(kick_response, nio.RoomKickResponse):
                # Successfully kicked, now remove all their thread invitations
                for state_key in thread_invitations.get(agent_name, []):
                    await self.client.room_put_state(room_id, THREAD_INVITE_EVENT_TYPE, {}, state_key)
                # Also remove their activity tracking
                await self.client.room_put_state(room_id, AGENT_ACTIVITY_EVENT_TYPE, {}, agent_name)
                removed_count += 1
                logger.info(f"Removed inactive agent {agent_name} from room {room_id}")

        return removed_count
