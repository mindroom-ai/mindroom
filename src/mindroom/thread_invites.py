"""Thread-specific agent invitation management using Matrix state events."""

from datetime import datetime, timedelta

import nio

from .logging_config import get_logger

logger = get_logger(__name__)

THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"
DEFAULT_TIMEOUT_HOURS = 24


class ThreadInviteManager:
    def __init__(self, client: nio.AsyncClient):
        self.client = client

    def _get_state_key(self, thread_id: str, agent_name: str) -> str:
        return f"{thread_id}:{agent_name}"

    async def add_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
        invited_by: str,
    ) -> None:
        await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={
                "invited_by": invited_by,
                "invited_at": datetime.now().isoformat(),
            },
            state_key=self._get_state_key(thread_id, agent_name),
        )

    async def get_thread_agents(self, thread_id: str, room_id: str) -> list[str]:
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            return []
        
        return [
            event.get("state_key", "").split(":", 1)[1]
            for event in response.events
            if event.get("type") == THREAD_INVITE_EVENT_TYPE
            and event.get("state_key", "").startswith(f"{thread_id}:")
        ]

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
        
        return [
            event.get("state_key", "").rsplit(":", 1)[0]
            for event in response.events
            if event.get("type") == THREAD_INVITE_EVENT_TYPE
            and event.get("state_key", "").endswith(f":{agent_name}")
        ]

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


    async def cleanup_inactive_agents(self, room_id: str, timeout_hours: int = DEFAULT_TIMEOUT_HOURS) -> int:
        """Remove agents who haven't responded in the room for timeout_hours."""
        state_response = await self.client.room_get_state(room_id)
        if not isinstance(state_response, nio.RoomGetStateResponse):
            return 0

        # Get invited agents with their invitation times
        invited_agents = []
        for event in state_response.events:
            if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                state_key = event.get("state_key", "")
                if ":" in state_key:
                    agent_name = state_key.split(":", 1)[1]
                    content = event.get("content", {})
                    if invited_at_str := content.get("invited_at"):
                        try:
                            invited_at = datetime.fromisoformat(invited_at_str)
                            invited_agents.append((state_key, agent_name, invited_at))
                        except (ValueError, TypeError):
                            pass

        if not invited_agents:
            return 0

        # Get last activity from room messages
        messages_response = await self.client.room_messages(room_id=room_id, start="", limit=1000)
        if not isinstance(messages_response, nio.RoomMessagesResponse):
            return 0

        # Map agent -> last message timestamp
        agent_last_activity = {}
        for event in messages_response.chunk:
            if isinstance(event, nio.RoomMessageText) and "@" in event.sender and ":mindroom.space" in event.sender:
                agent_name = event.sender.split("@")[1].split(":")[0]
                if agent_name not in agent_last_activity:
                    agent_last_activity[agent_name] = datetime.fromtimestamp(event.server_timestamp / 1000)

        # Remove inactive agents
        removed_count = 0
        now = datetime.now()
        threshold = timedelta(hours=timeout_hours)

        for state_key, agent_name, invited_at in invited_agents:
            last_activity = agent_last_activity.get(agent_name, invited_at)
            if now - last_activity > threshold:
                # Kick agent and remove invitation
                if isinstance(await self.client.room_kick(room_id, f"@{agent_name}:mindroom.space", "Inactive"), nio.RoomKickResponse):
                    await self.client.room_put_state(room_id, THREAD_INVITE_EVENT_TYPE, {}, state_key)
                    removed_count += 1

        return removed_count
